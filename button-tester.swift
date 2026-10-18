// Prints every mouse button press macOS delivers to apps. Run: swift button-tester.swift
// Needs Input Monitoring permission for the terminal. Ctrl+C to quit.
import CoreGraphics
import Foundation

let watchedEvents: [CGEventType] = [.leftMouseDown, .rightMouseDown, .otherMouseDown]
let eventMask = watchedEvents.reduce(CGEventMask(0)) { mask, eventType in
    mask | (CGEventMask(1) << eventType.rawValue)
}

let printButtonPress: CGEventTapCallBack = { _, _, event, _ in
    // macOS numbers buttons from 0 (left = 0); print the 1-based number omm.py uses.
    let zeroBasedButton = event.getIntegerValueField(.mouseEventButtonNumber)
    print("mouse button \(zeroBasedButton + 1)")
    return Unmanaged.passUnretained(event)
}

guard let eventTap = CGEvent.tapCreate(
    tap: .cgSessionEventTap,
    place: .headInsertEventTap,
    options: .listenOnly,
    eventsOfInterest: eventMask,
    callback: printButtonPress,
    userInfo: nil
) else {
    print("Could not listen for mouse events. Grant Input Monitoring to your terminal, then rerun.")
    exit(1)
}

let runLoopSource = CFMachPortCreateRunLoopSource(nil, eventTap, 0)
CFRunLoopAddSource(CFRunLoopGetCurrent(), runLoopSource, .commonModes)
print("Press mouse buttons (Ctrl+C to quit)...")
CFRunLoopRun()
