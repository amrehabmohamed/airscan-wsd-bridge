# Contributing

The goal is simple: make more WSD-only scanners work natively on macOS. The most useful contributions are device reports and fixes for devices that do not work yet.

## Report a device

Open an issue with the **Device report** template, whether it worked or not. Include the output of:

```bash
python3 airscan_wsd.py discover
```

and, if a scan failed, the relevant lines of `~/Library/Logs/airscan-wsd-bridge.log`.

Before posting, remove anything that identifies your device or network: IP addresses, serial numbers, MAC addresses and `urn:uuid:` values. Model name, firmware version and capabilities are what matter.

## Send a change

1. Fork the repository and create a branch.
2. Make the change. Keep it small and focused on one thing.
3. Run the offline tests: `python3 -m unittest discover tests`
4. If you changed how the device is driven, test against a real scanner and say which model and macOS version in the pull request.
5. Open a pull request against `main`.

Every pull request is reviewed and merged by the maintainer; nobody else can merge. Expect questions, especially for changes that cannot be verified on the maintainer's hardware.

## Ground rules

- **Standard library only.** No third-party packages, no build step. Anyone should be able to read the whole thing and run it with the Python that ships with the Xcode Command Line Tools (3.9).
- **Nothing device-specific is hardcoded.** No addresses, serials, UUIDs or model names in the code. If a device needs special handling, key it on what the device reports about itself.
- **Be careful with scan tickets.** Tickets are derived from the device's own default ticket for a reason: a rejected ticket has left a scanner stuck until it was power-cycled. Do not send a job to a device that is not idle.
- **No new network exposure.** The bridge only serves the Mac it runs on. Changes that open it to other machines, download or execute anything, or send data anywhere other than the scanner will not be merged.
- **Add a row to the tested-devices table** in the README when you confirm a new device.

## Ideas that would help

- Document feeder (ADF) and duplex support.
- Streaming the image to macOS as it arrives, so there is a live preview.
- Using the device's own scan region instead of cropping on the Mac, where the device handles it safely.
- More than one scanner per Mac.
