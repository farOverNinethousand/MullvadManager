# MullvadManager
Library to control a Mullvad instance via CLI commands

Mullvad CLI commands:  
https://mullvad.net/de/help/how-use-mullvad-cli

This thing was initially built to work with Mullvad CLI version: mullvad-cli 2024.5

```
async def main():
    if not is_mullvad_supported():
        print("Mullvad is not installed or not in OS PATH!")
        return
    mm = MullvadManager()
    await mm.initialize()
    await mm.connect_to_random_server(country="de")
    mm.close()
```

More exemplaric code see main function of MullvadManager.py class.