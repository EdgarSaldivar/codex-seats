import SwiftUI

public struct ContentView: View {
    @State private var count = 0

    public var body: some View {
        VStack(spacing: 24) {
            Text("Development demo")
                .font(.largeTitle)
                .foregroundStyle(.orange)
            Text("Tap count: \(count)")
                .font(.title2)
                .accessibilityIdentifier("tap-count")
            Button("Add one") { count += 1 }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("add-one")
        }
        .padding()
    }

    public init() {}
}
