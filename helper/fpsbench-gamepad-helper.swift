// fpsbench virtual gamepad helper (CoreHID HIDVirtualDevice, macOS 15+).
//
// This process exists for one reason: the virtual-HID entitlement can only be
// carried by a *signed binary*, not by the python interpreter that runs
// fpsbench.  It speaks a line-oriented JSON protocol with the Python side
// (fpsbench/backends/macos/gamepad.py) and does nothing else:
//
//   -> {"cmd":"hello"}                      <- {"ok":true,"device":"..."}
//   -> {"cmd":"state","lx":0.5,...}          <- {"ok":true}
//   -> {"cmd":"neutral"}                     <- {"ok":true}
//
// Build: helper/build.sh   (requires the com.apple.developer.hid.virtual.device
// entitlement granted to your signing identity - see helper/README.md)
//
// The report descriptor is a plain 15-button / 6-axis game pad.  It is
// deliberately not a DS4/DualSense descriptor: emulating another vendor's
// device would require its exact report format, and a generic game pad is what
// most engines accept.  Swap the descriptor if a target game needs more.

import CoreHID
import Foundation

/// 15 buttons (SDL's order) + 1 pad bit, then X, Y, Z, Rz, Rx, Ry as int16 LE.
private let reportDescriptor = Data([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,  // Usage Page (Generic Desktop), Usage (Game Pad), Collection (Application)
    0x05, 0x09, 0x19, 0x01, 0x29, 0x0F,  //   Usage Page (Button), Usage Min 1, Usage Max 15
    0x15, 0x00, 0x25, 0x01,              //   Logical Min 0, Logical Max 1
    0x75, 0x01, 0x95, 0x0F, 0x81, 0x02,  //   Report Size 1, Count 15, Input (Data,Var,Abs)
    0x75, 0x01, 0x95, 0x01, 0x81, 0x03,  //   one padding bit
    0x05, 0x01,                          //   Usage Page (Generic Desktop)
    0x09, 0x30, 0x09, 0x31, 0x09, 0x32,  //   X, Y, Z
    0x09, 0x33, 0x09, 0x34, 0x09, 0x35,  //   Rx, Ry, Rz
    0x16, 0x00, 0x80, 0x26, 0xFF, 0x7F,  //   Logical Min -32768, Max 32767
    0x75, 0x10, 0x95, 0x06, 0x81, 0x02,  //   Report Size 16, Count 6, Input
    0xC0,                                // End Collection
])

/// Report layout offsets.
private let buttonOffset = 0
private let axisOffset = 2
private let reportSize = 14

/// SDL_GameControllerButton order, so the same button names mean the same bit.
private let buttonNames = [
    "a", "b", "x", "y", "back", "guide", "start", "leftstick", "rightstick",
    "leftshoulder", "rightshoulder", "dpup", "dpdown", "dpleft", "dpright",
]

final class ReportDelegate: HIDVirtualDeviceDelegate, @unchecked Sendable {
    func hidVirtualDevice(
        _ device: HIDVirtualDevice, receivedSetReportRequestOfType type: HIDReportType,
        id: HIDReportID?, data: Data
    ) async throws {}

    func hidVirtualDevice(
        _ device: HIDVirtualDevice, receivedGetReportRequestOfType type: HIDReportType,
        id: HIDReportID?, maxSize: Int
    ) async throws -> Data {
        Data(count: min(maxSize, reportSize))
    }
}

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

private func clamp(_ value: Double, _ low: Double, _ high: Double) -> Double {
    min(high, max(low, value))
}

/// Build one input report from a decoded `state`/`neutral` command.
private func buildReport(_ message: [String: Any]) -> Data {
    var report = Data(count: reportSize)
    let buttons = message["buttons"] as? [String] ?? []
    var bits: UInt16 = 0
    for name in buttons {
        if let index = buttonNames.firstIndex(of: name.lowercased()) {
            bits |= UInt16(1) << UInt16(index)
        }
    }
    report[buttonOffset] = UInt8(truncatingIfNeeded: bits)
    report[buttonOffset + 1] = UInt8(truncatingIfNeeded: bits >> 8)

    func axis(_ key: String, trigger: Bool) -> Int16 {
        let value = (message[key] as? Double) ?? 0
        let clamped = trigger ? clamp(value, 0, 1) : clamp(value, -1, 1)
        // Sticks keep SDL's y-down convention: write the value as-is.
        return Int16((clamped * 32767.0).rounded())
    }

    let values: [Int16] = [
        axis("lx", trigger: false),
        axis("ly", trigger: false),
        axis("rx", trigger: false),
        axis("ry", trigger: false),
        axis("l2", trigger: true),
        axis("r2", trigger: true),
    ]
    for (index, value) in values.enumerated() {
        let offset = axisOffset + index * 2
        let raw = UInt16(bitPattern: value)
        report[offset] = UInt8(truncatingIfNeeded: raw)
        report[offset + 1] = UInt8(truncatingIfNeeded: raw >> 8)
    }
    return report
}

private func reply(_ object: [String: Any]) {
    guard let line = try? JSONSerialization.data(withJSONObject: object, options: []),
          var text = String(data: line, encoding: .utf8)
    else { return }
    text += "\n"
    FileHandle.standardOutput.write(Data(text.utf8))
}

@main
struct Helper {
    static func main() async {
        let vendorID = UInt32(ProcessInfo.processInfo.environment["FPSBENCH_GP_VID"] ?? "") ?? 0x1209
        let productID = UInt32(ProcessInfo.processInfo.environment["FPSBENCH_GP_PID"] ?? "") ?? 0x0001
        let properties = HIDVirtualDevice.Properties(
            descriptor: reportDescriptor,
            vendorID: vendorID,
            productID: productID,
            transport: .virtual,
            product: "fpsbench virtual gamepad",
            manufacturer: "fpsbench",
            versionNumber: 1,
            serialNumber: "fpsbench-0001"
        )
        guard let device = HIDVirtualDevice(properties: properties) else {
            // The usual reason: the entitlement is missing or not granted to this signature.
            fail("FAILED to create the virtual HID device: missing com.apple.developer.hid.virtual.device?")
        }
        await device.activate(delegate: ReportDelegate())

        // A first neutral report so the device has a defined state.
        try? await device.dispatchInputReport(data: buildReport([:]), timestamp: SuspendingClock().now)

        while let line = readLine(strippingNewline: true) {
            guard let data = line.data(using: .utf8),
                  let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let command = message["cmd"] as? String
            else {
                reply(["ok": false, "error": "invalid JSON"])
                continue
            }
            switch command {
            case "hello":
                reply(["ok": true, "device": "CoreHID virtual gamepad (\(String(format: "%04X:%04X", vendorID, productID)))"])
            case "state":
                do {
                    try await device.dispatchInputReport(
                        data: buildReport(message), timestamp: SuspendingClock().now)
                    reply(["ok": true])
                } catch {
                    reply(["ok": false, "error": "\(error)"])
                }
            case "neutral":
                do {
                    try await device.dispatchInputReport(
                        data: buildReport([:]), timestamp: SuspendingClock().now)
                    reply(["ok": true])
                } catch {
                    reply(["ok": false, "error": "\(error)"])
                }
            case "quit":
                reply(["ok": true])
                exit(0)
            default:
                reply(["ok": false, "error": "unknown command"])
            }
        }
    }
}
