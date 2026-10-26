// Prints {"found": {"Town, ST": [lat, lon], "@lat,lon": "Neighborhood, ST"}, "unknown": [...]} for the arguments.
// Coordinate arguments start with "@" (e.g. "@34.173,-118.437") and are reverse-geocoded into a neighborhood/city name.
// Apple throttles bursts, so lookups are spaced out and the first other error stops the batch;
// names after it are left out of both lists and retried on a later run.
// Build: swiftc -O geocode.swift -o bin/geocode
import CoreLocation
import Foundation

let geocoder = CLGeocoder()
let names = Array(CommandLine.arguments.dropFirst())
var found: [String: Any] = [:]
var unknown: [String] = []
var done = false

func formatPlace(_ p: CLPlacemark) -> String? {
    let state = (p.administrativeArea ?? "").trimmingCharacters(in: .whitespaces)
    let city = (p.locality ?? "").trimmingCharacters(in: .whitespaces)
    let sub = (p.subLocality ?? "").trimmingCharacters(in: .whitespaces)
    guard !state.isEmpty else { return nil }
    if (city.isEmpty || city.lowercased() == "los angeles") && !sub.isEmpty {
        return "\(sub), \(state)"
    }
    if !city.isEmpty {
        return "\(city), \(state)"
    }
    return nil
}

func parseCoord(_ raw: String) -> CLLocation? {
    guard raw.hasPrefix("@") else { return nil }
    let parts = raw.dropFirst().split(separator: ",")
    guard parts.count == 2, let lat = Double(parts[0]), let lon = Double(parts[1]) else { return nil }
    return CLLocation(latitude: lat, longitude: lon)
}

func resolve(_ index: Int) {
    if index >= names.count { done = true; return }
    let arg = names[index]
    let handler: ([CLPlacemark]?, Error?) -> Void = { places, error in
        if arg.hasPrefix("@") {
            if let p = places?.first, let label = formatPlace(p) {
                found[arg] = label
            } else if let e = error as? CLError, e.code == .geocodeFoundNoResult {
                unknown.append(arg)
            } else if error != nil {
                done = true; return
            }
        } else {
            if let c = places?.first?.location?.coordinate {
                found[arg] = [c.latitude, c.longitude]
            } else if let e = error as? CLError, e.code == .geocodeFoundNoResult {
                unknown.append(arg)
            } else if error != nil {
                done = true; return
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.3) { resolve(index + 1) }
    }
    if let loc = parseCoord(arg) {
        geocoder.reverseGeocodeLocation(loc, completionHandler: handler)
    } else {
        geocoder.geocodeAddressString(arg, completionHandler: handler)
    }
}

resolve(0)
while !done { RunLoop.current.run(until: Date().addingTimeInterval(0.05)) }
let output: [String: Any] = ["found": found, "unknown": unknown]
print(String(data: try! JSONSerialization.data(withJSONObject: output), encoding: .utf8)!)
