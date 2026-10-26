// Prints {"found": {"Town, ST": [lat, lon]}, "unknown": ["Town, ST"]} for the arguments.
// Apple throttles bursts, so lookups are spaced out and the first other error stops the batch;
// names after it are left out of both lists and retried on a later run.
// Build: swiftc -O geocode.swift -o bin/geocode
import CoreLocation
import Foundation

let geocoder = CLGeocoder()
let names = Array(CommandLine.arguments.dropFirst())
var found: [String: [Double]] = [:]
var unknown: [String] = []
var done = false

func resolve(_ index: Int) {
    if index >= names.count { done = true; return }
    geocoder.geocodeAddressString(names[index]) { places, error in
        if let c = places?.first?.location?.coordinate {
            found[names[index]] = [c.latitude, c.longitude]
        } else if let e = error as? CLError, e.code == .geocodeFoundNoResult {
            unknown.append(names[index])
        } else if error != nil {
            done = true; return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.3) { resolve(index + 1) }
    }
}

resolve(0)
while !done { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
let output: [String: Any] = ["found": found, "unknown": unknown]
print(String(data: try! JSONSerialization.data(withJSONObject: output), encoding: .utf8)!)
