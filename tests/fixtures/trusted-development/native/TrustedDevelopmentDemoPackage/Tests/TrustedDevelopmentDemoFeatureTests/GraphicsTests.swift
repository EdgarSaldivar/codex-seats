import CoreVideo
import Foundation
import Testing

@Test func iosurfacePixelBufferCanBeCreated() throws {
    var pixel: CVPixelBuffer?
    let attributes = [kCVPixelBufferIOSurfacePropertiesKey as String: [:]] as CFDictionary
    let status = CVPixelBufferCreate(kCFAllocatorDefault, 32, 32,
        kCVPixelFormatType_32BGRA, attributes, &pixel)
    print("IOSurface pixel status: \(status)")
    #expect(status == kCVReturnSuccess)
    #expect(pixel != nil)
}
