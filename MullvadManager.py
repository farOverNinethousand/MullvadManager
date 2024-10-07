import asyncio
import re
import subprocess
import random
import time
import logging
from typing import Union, Optional, List
from enum import Enum

import httpx


class MullvadException(Exception):
    """Exception for general Mullvad errors."""
    pass


class MullvadStatus(Enum):
    CONNECTED = 1
    CONNECTING = 2
    DISCONNECTED = 3
    UNKNOWN = 4
    RECONNECTING = 5
    ERROR = 6
    INVALID_CREDENTIALS = 7
    ACCOUNT_EXPIRED = 8


async def cli_command(command: List[str], retries: int = 10) -> str:
    """Runs a CLI command with up to 10 retries if stderr indicates a potential JSON error."""
    attempt = 0

    while attempt < retries:
        attempt += 1
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            return stdout.decode().strip()

        stderr_str = stderr.decode().strip()
        logging.error(f"Attempt {attempt} failed with error: {stderr_str}")

        # Check if stderr looks like a JSON error (starts with '{')
        if "gRPC call returned error" in stderr_str:
            logging.warning(f"JSON-like error in stderr. Retrying... (Attempt {attempt}/{retries})")
            await asyncio.sleep(1)  # Optional: Add a delay between retries
        else:
            raise MullvadException(f"CLI command failed after {attempt} attempts: {stderr_str}")

    raise MullvadException(f"CLI command failed after {retries} retries.")


async def get_mullvad_version() -> str:
    """Fetches the Mullvad version using the CLI."""
    return await cli_command(["mullvad", "--version"])


def get_mullvad_version2() -> Optional[str]:
    # TODO: Get rid of this
    """Fetches the Mullvad version using the CLI synchronously."""
    result = subprocess.run(["mullvad", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    else:
        raise MullvadException(f"Failed to get Mullvad version: {result.stderr.strip()}")


def is_mullvad_supported() -> bool:
    """Checks if Mullvad is installed and accessible by checking its version."""
    try:
        return get_mullvad_version2() is not None
    except MullvadException:
        return False


def looksLikeValidMullvadAccountID(account_id: Union[None, str]) -> bool:
    if account_id is None:
        return False
    """
    Checks if the provided account_id looks like a valid Mullvad account ID.

    :param account_id: The account ID to check.
    :return: True if the account ID is valid, False otherwise.
    """
    # Check if the account_id is a string of exactly 16 digits
    return bool(re.fullmatch(r"\d{16}", account_id))


KNOWN_WORKING_MULLVAD_VERSIONS = ["mullvad-cli 2024.5"]


class MullvadManager:

    def __init__(self, initialize_now=False, account_id: Union[str, None] = None):
        """
        DO NOT USE initialize_now!!

        """
        self.initialVersion = None
        self.initialLoginState = False
        self.initialConnectState = False
        self.initialServer = None
        self.restoreInitialStateOnExit = True
        self.server_ip_mapping = {}

        # Initialize an async HTTPX client for reuse across requests
        self.http_client = httpx.AsyncClient()

        if initialize_now:
            try:
                # Check if there is already a running event loop
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No event loop running -> use asyncio.run to initialize
                asyncio.run(self.initialize())
            else:
                # Event loop is already running -> use create_task to run the initializer
                loop.create_task(self.initialize())
        self.account_id = account_id

    async def initialize(self):
        if not is_mullvad_supported():
            raise MullvadException("Mullvad does not seem to be installed or the path is not set correctly or not at all.")

        self.initialVersion = await get_mullvad_version()
        if self.initialVersion not in KNOWN_WORKING_MULLVAD_VERSIONS:
            # Warning for developer
            logging.warning(
                f"Your Mullvad version {self.initialVersion} is not in the list of known/working CLI versions: {KNOWN_WORKING_MULLVAD_VERSIONS} | SOME THINGS MAY NOT WORK!!")

        self.initialLoginState = await self.is_logged_in()
        if self.initialLoginState:
            self.initialConnectState = await self.isConnected()
            self.initialServer = await self.get_current_server()
            if self.initialConnectState:
                await self.update_server_ip_mapping()
        else:
            logging.warning("You are not logged in! You can't do much in this state!!")
        logging.debug("Init complete")
        # End

    def looksLikeValidMullvadAccountID(self, account_id: str) -> bool:
        """
        Checks if the provided account_id looks like a valid Mullvad account ID.

        :param account_id: The account ID to check.
        :return: True if the account ID is valid, False otherwise.
        """
        # Check if the account_id is a string of exactly 16 digits
        return bool(re.fullmatch(r"\d{16}", account_id))

    async def getStatusStr(self) -> Optional[str]:
        """Returns the current Mullvad status as a string, or None if unsuccessful."""
        return await cli_command(["mullvad", "status"])

    async def getStatus(self) -> MullvadStatus:
        """Returns the MullvadStatus enum based on the status string."""
        status_str = (await self.getStatusStr()).lower()

        if "disconnected" in status_str:
            return MullvadStatus.DISCONNECTED
        elif "connected" in status_str:
            return MullvadStatus.CONNECTED
        elif "reconnecting" in status_str:
            return MullvadStatus.RECONNECTING
        elif "connecting" in status_str:
            return MullvadStatus.CONNECTING
        elif "invalid account" in status_str:
            return MullvadStatus.INVALID_CREDENTIALS
        elif "account expired" in status_str:
            return MullvadStatus.ACCOUNT_EXPIRED
        elif "error" in status_str:
            return MullvadStatus.ERROR
        else:
            return MullvadStatus.UNKNOWN

    async def wait_for_status(self, desired_status: Union[MullvadStatus, List[MullvadStatus]]) -> bool:
        """Waits up to 30 seconds for the status to reach an allowed MullvadStatus."""
        if isinstance(desired_status, list):
            allowedStatusList = desired_status
        else:
            allowedStatusList = [desired_status]
        for _ in range(30):
            if await self.getStatus() in allowedStatusList:
                return True
            await asyncio.sleep(1)
        return False

    async def isConnected(self) -> bool:
        """Checks if Mullvad is connected."""
        return await self.getStatus() == MullvadStatus.CONNECTED

    async def fetch_server_list(self) -> List[str]:
        """ Returns a list of active(!) VPN servers.
         Status page on website: https://mullvad.net/en/servers
         """
        allservers = []
        servers_by_country = {}
        # 2024-09-20: CLI  does not return server status so API is more useful
        useCLI = False
        # self.server_ip_mapping.clear()
        offlineServers = []
        otherSkippedServers = []
        totalNumberofServers = 0
        if useCLI:
            """Parses the CLI output of 'mullvad relay list' to extract servers and their IPs, avoiding duplicates."""
            output = (await cli_command(["mullvad", "relay", "list"])).splitlines()
            for line in output:
                line = line.strip()
                if 'hosted by' not in line:
                    continue
                infos = line.split(' ')
                hostname = infos[0]
                serverstrings = hostname.split('-')
                country_code = serverstrings[0]
                server_type = serverstrings[2]
                if server_type == 'br':
                    # Skip bridges
                    otherSkippedServers.append(hostname)
                    continue
                serverlist = servers_by_country.setdefault(country_code, [])
                serverlist.append(hostname)
                allservers.append(hostname)

                iptext = re.search(r"\(([^)]+)", line).group(1)
                ip_list = iptext.split(", ")

                unique_ips = set(self.server_ip_mapping.setdefault(hostname, []))
                unique_ips.update(ip_list)
                self.server_ip_mapping[hostname] = list(unique_ips)
                totalNumberofServers += 1
        else:
            # API, source: https://www.reddit.com/r/mullvadvpn/comments/o9jewj/downloadable_server_list_like_a_csv_file/
            try:
                response = await self.http_client.get("https://api.mullvad.net/www/relays/all/", timeout=60)
                arraylist: List[dict] = response.json()
            except httpx.RequestError as e:
                logging.error(f"HTTP Request failed: {e}")
                raise MullvadException("Failed to fetch the server list from the Mullvad API")
            except ValueError as e:
                logging.error(f"Invalid JSON response: {e}")
                raise MullvadException("Failed to parse the server list from the Mullvad API")
            totalNumberofServers = len(arraylist)
            for serverdict in arraylist:
                hostname = serverdict['hostname']
                if not serverdict['active']:
                    # Skip inactive servers
                    offlineServers.append(hostname)
                    continue
                if serverdict['type'] == 'bridge':
                    # Skip bridges
                    otherSkippedServers.append(hostname)
                    continue
                country_code = serverdict['country_code']
                serverlist = servers_by_country.setdefault(country_code, [])
                serverlist.append(hostname)
                allservers.append(hostname)

                ip_list = []
                ipv4_addr_in = serverdict.get('ipv4_addr_in')
                if ipv4_addr_in is not None:
                    ip_list.append(ipv4_addr_in)
                ipv6_addr_in = serverdict.get('ipv6_addr_in')
                if ipv6_addr_in is not None:
                    ip_list.append(ipv6_addr_in)

                unique_ips = set(self.server_ip_mapping.setdefault(hostname, []))
                unique_ips.update(ip_list)
                self.server_ip_mapping[hostname] = list(unique_ips)

        logging.debug(
            f"Active servers: {len(self.server_ip_mapping)}/{totalNumberofServers} | Currently offline servers: {len(offlineServers)}/{totalNumberofServers} | Other skipped Servers: {len(otherSkippedServers)}/{totalNumberofServers}")

        return allservers

    async def get_servers(self, country: Optional[str] = None) -> List[str]:
        """Returns a list of Mullvad servers, optionally filtered by country."""
        servers = await self.fetch_server_list()
        if country:
            country = country.lower()
            servers_by_country = []
            for hostname in servers:
                if hostname.startswith(country):
                    servers_by_country.append(hostname)
            return servers_by_country
        else:
            return servers

    async def get_server_by_ip(self, ip_address: str) -> Optional[str]:
        """Returns the first server that has the given IP address in its IP list, or None if not found."""
        i = 0
        while i < 2:
            if i == 1:
                # No result based on our cached mapping -> Re-Fetch list of servers which will update the mapping
                await self.fetch_server_list()
            for server, ip_list in self.server_ip_mapping.items():
                if ip_address in ip_list:
                    return server
            i += 1
        logging.warning(f"No server found for IP {ip_address}.")
        return None

    async def get_current_server(self) -> Union[str, None]:
        """Returns the currently connected Mullvad server."""
        if not await self.isConnected():
            return None

        status = await self.getStatusStr()
        for line in status.split("\n"):
            if "Connected to " in line:
                return line.split("Connected to ")[1].split(" ")[0]
        return None

    async def update_server_ip_mapping(self) -> None:
        """Updates the mapping from server name to external IP address."""
        server = await self.get_current_server()
        if not server:
            logging.info("No server is connected.")
            return

        ip = await self.getCurrentIPAddress()
        ip_list = self.server_ip_mapping.setdefault(server, [])
        if ip not in ip_list:
            ip_list.append(ip)

    async def get_ips_for_server(self, server: str) -> Union[list, None]:
        """Returns a list of IP addresses for a specific server, if available."""
        return self.server_ip_mapping.get(server, None)

    async def get_current_ip(self) -> Union[str, None]:
        """Returns the first current IP of the connected VPN server."""
        current_server = await self.get_current_server()
        if current_server:
            ip_list = await self.get_ips_for_server(current_server)
            if ip_list:
                return ip_list[-1]
        return None

    async def ensure_login(self) -> None:
        """Ensure that the user is logged in, otherwise raise an exception."""
        if not await self.is_logged_in():
            raise MullvadException("User is not logged into Mullvad. Please log in before attempting to connect.")

    async def ensure_valid_account_id(self, account_id: Union[str, None]):
        if not looksLikeValidMullvadAccountID(account_id):
            raise MullvadException("account_id validation failed")

    async def ensure_server_exists(self, server: str):
        servers = await self.get_servers()
        if server not in servers:
            raise MullvadException(f"The server '{server}' is not in the list of available servers.")

    async def connect(self) -> None:
        """Connects to Mullvad."""
        await self.ensure_login()
        if await self.isConnected():
            logging.info("Already connected -> Doing nothing")
            return
        await cli_command(["mullvad", "connect"])
        await self.wait_for_status(MullvadStatus.CONNECTED)
        await self.update_server_ip_mapping()

    async def disconnect(self) -> None:
        """Disconnects from Mullvad."""
        if await self.getStatus() == MullvadStatus.DISCONNECTED:
            logging.info("Already disconnected -> Doing nothing")
            return
        await cli_command(["mullvad", "disconnect"])
        await self.wait_for_status(MullvadStatus.DISCONNECTED)

    async def set_server(self, server: str, check_server_existence: bool = True) -> None:
        """Sets the specified server."""
        await self.ensure_login()
        currentServer = await self.get_current_server()
        if currentServer == server:
            logging.info(f"Server {server} is already set -> Doing nothing")
            return
        if check_server_existence:
            await self.ensure_server_exists(server)
        isAlreadyConnected = await self.isConnected()
        await cli_command(["mullvad", "relay", "set", "location", server])
        if isAlreadyConnected:
            # Wait for connection to be established
            await self.wait_for_status(MullvadStatus.CONNECTED)
            await self.update_server_ip_mapping()

    async def connect_to_server(self, server: str, check_server_existence: bool = True) -> None:
        """Connects to the specified Mullvad server."""
        await self.ensure_login()
        if await self.get_current_server() == server and await self.isConnected():
            logging.info(f"Already connected to server {server}")
            return
        if check_server_existence:
            await self.ensure_server_exists(server)
        isAlreadyConnected = await self.isConnected()
        await self.set_server(server)
        if not isAlreadyConnected:
            await self.connect()

    async def connect_to_random_server(self, country: Optional[str] = None, excludeServers: Union[list, str] = None) -> None:
        """Randomly selects a server and connects, optionally filtered by country."""
        if isinstance(excludeServers, str):
            # Single string -> Put it into list
            excludeServers = [excludeServers]

        available_servers = await self.get_servers(country)
        if available_servers is None or len(available_servers) == 0:
            raise MullvadException("No available servers.")
        if excludeServers is not None:
            for server in excludeServers:
                available_servers.remove(server)
            if len(available_servers) == 0:
                logging.error(f"All available servers were excluded: {excludeServers}")
                raise MullvadException(f"No available servers because all available servers were excluded: {excludeServers}")
        selected_server = random.choice(available_servers)
        await self.connect_to_server(selected_server, check_server_existence=False)

    async def ensure_connected_to_country(self, country: str) -> None:
        """Ensure connection to any server in the specified country. If not connected, connect to a random server in that country."""
        country = country.lower()
        current_server = await self.get_current_server()

        # Get the list of available servers for the specified country
        available_servers = await self.get_servers(country)
        if available_servers is None or len(available_servers) == 0:
            raise MullvadException(f"No available servers for country: {country}")

        # Check if currently connected to a server in the given country
        if current_server:
            current_country_code = current_server.split('-')[0]
            if current_country_code == country:
                logging.info(f"Already connected to a server in {country}.")
                return

        # Not connected to the desired country, connect to a random server from the available ones
        selected_server = random.choice(available_servers)
        logging.info(f"Connecting to a random server in {country} -> {selected_server}")
        await self.connect_to_server(selected_server)

    async def restoreInitialConnectState(self) -> None:
        """Restores the initial connection state."""
        if self.initialConnectState:
            if not await self.isConnected():
                logging.info("Restoring connection as it was initially connected.")
                # First set server, then connect to speed up process otherwise we may waste time
                await self.setInitialServer()
                await self.connect()
            else:
                logging.info("Already connected; no action needed.")
                await self.setInitialServer()
                return
        else:
            if await self.isConnected():
                logging.info("Disconnecting as it was initially disconnected.")
                # First disconnect, then set server to not waste time
                await self.disconnect()
                await self.setInitialServer()
                return
            else:
                logging.info("Already disconnected; no action needed.")
                await self.setInitialServer()
                return

    async def setInitialServer(self):
        """ Sets initially set server if this isn't already our current set server. """
        currentServer = await self.get_current_server()
        if self.initialServer is not None and currentServer != self.initialServer:
            logging.info(f"Setting initial server: {self.initialServer}")
            await self.set_server(self.initialServer)

    async def is_logged_in(self, account_number: Optional[str] = None) -> bool:
        """Checks whether the user is logged in into a Mullvad account."""
        login_info = await cli_command(["mullvad", "account", "get"])
        if account_number and account_number in login_info:
            return True
        elif "Mullvad account:" in login_info:
            return True
        else:
            return False

    async def login(self, account_id: Union[str, None]) -> None:
        # TODO: Test this
        """
        Logs in using the provided Mullvad account ID.

        :param account_id: The Mullvad account ID to log in with.
        :raises MullvadException: If login fails.
        """
        if account_id is None:
            # Use globally given account_id
            account_id = self.account_id
        await self.ensure_valid_account_id(account_id)

        # Log in using the given account ID
        logging.info(f"Logging in with account: {account_id}")
        await cli_command(["mullvad", "account", "login", account_id])

        # Verify the login by checking the current account
        if not await self.is_logged_in(account_id):
            raise MullvadException(f"Failed to log in with account: {account_id}")

        logging.info(f"Successfully logged in with account: {account_id}")

    async def factory_reset(self) -> None:
        """Runs a factory reset on Mullvad."""
        logging.info("Performing Mullvad factory reset")
        await cli_command(["mullvad", "factory-reset", "-y"])

    async def getCurrentIPAddress(self, timeoutSeconds: int = 30) -> str:
        """Fetches the current IP address using Mullvad's API, retrying for up to userDefined seconds."""
        start_time = time.time()
        while time.time() - start_time < timeoutSeconds:
            try:
                response = await self.http_client.get('https://am.i.mullvad.net/json', timeout=5.0)
                response.raise_for_status()
                data = response.json()
                return data['ip']
            except (httpx.RequestError, httpx.HTTPStatusError):
                await asyncio.sleep(1)
        raise MullvadException(f"Failed to retrieve IP address within {timeoutSeconds} seconds.")

    async def close(self):
        """Closes the HTTP client."""
        await self.http_client.aclose()


async def main():
    if not is_mullvad_supported():
        print("Mullvad is not installed or not in OS PATH!")
        return
    mm = MullvadManager()
    await mm.initialize()

    try:
        logging.info(f"Server-to-IP mapping at start: {mm.server_ip_mapping}")

        await mm.connect_to_random_server()

        server_ip = await mm.get_current_ip()
        if server_ip:
            # Demonstration of internal server:IP mapping
            logging.info(f"Current VPN IP: {server_ip}")
            server_name = await mm.get_server_by_ip(server_ip)
            logging.info(f"Server found for IP {server_ip}: {server_name}" if server_name else f"No server found for IP {server_ip}")

        random_server = random.choice(await mm.get_servers())
        logging.info(f"Connecting to specific server: {random_server}")
        await mm.connect_to_server(random_server)

        await mm.connect_to_random_server(country="de")

        logging.info(f"Server-to-IP mapping after connecting to {random_server}: {mm.server_ip_mapping}")
        await mm.restoreInitialConnectState()
        logging.info(f"Final server-to-IP mapping: {mm.server_ip_mapping}")
    finally:
        await mm.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
