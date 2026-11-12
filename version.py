# Single source of truth for the app version.
# Read by the GUI (About box, update check) and by build_installer.ps1
# (passed to Inno Setup). Bump this, rebuild, and publish a GitHub Release
# tagged v<APP_VERSION> with the Setup .exe attached to ship an update.
APP_VERSION = "1.2"
