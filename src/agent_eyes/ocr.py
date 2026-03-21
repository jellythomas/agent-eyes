"""Cross-platform OCR engines for agent-eyes.

Returns text blocks with bounding boxes for coordinate-based interaction.
Uses platform-native APIs where available (zero extra deps on macOS/Windows).

Platform support:
  - macOS:   Apple Vision framework (VNRecognizeTextRequest)
  - Windows: Windows.Media.Ocr (UWP OCR)
  - Linux:   pytesseract (requires tesseract-ocr system package)
"""
from __future__ import annotations

import abc
import sys
import logging
from dataclasses import dataclass

logger = logging.getLogger("agent-eyes")


@dataclass
class OCRHint:
    """A text block found by OCR with its screen-space bounding box."""
    text: str
    x: int          # Screen X in points (not pixels)
    y: int          # Screen Y in points
    width: int      # Width in points
    height: int     # Height in points
    confidence: float  # 0.0 - 1.0


class OCREngine(abc.ABC):
    """Abstract OCR engine interface."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        ...

    @abc.abstractmethod
    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        """Run OCR on PNG image data. Returns text hints with screen-space coordinates."""
        ...


class MacOSOCR(OCREngine):
    """macOS OCR via Apple Vision framework."""

    def is_available(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            import Vision  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        import Vision
        import Quartz
        from Foundation import NSData

        # Load image from PNG data
        ns_data = NSData.dataWithBytes_length_(image_data, len(image_data))
        cg_source = Quartz.CGImageSourceCreateWithData(ns_data, None)
        if cg_source is None:
            return []
        cg_image = Quartz.CGImageSourceCreateImageAtIndex(cg_source, 0, None)
        if cg_image is None:
            return []

        img_w = Quartz.CGImageGetWidth(cg_image)
        img_h = Quartz.CGImageGetHeight(cg_image)

        # Run text recognition
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {})
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(1)  # 0=fast, 1=accurate
        request.setUsesLanguageCorrection_(True)

        success, error = handler.performRequests_error_([request], None)
        if not success:
            logger.error("Vision OCR failed: %s", error)
            return []

        hints = []
        for observation in request.results():
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = candidates[0].string()
            conf = candidates[0].confidence()

            # Vision returns normalized coords (0-1, origin bottom-left)
            bbox = observation.boundingBox()
            # Convert to pixel coords (origin top-left)
            px_x = bbox.origin.x * img_w
            px_y = (1.0 - bbox.origin.y - bbox.size.height) * img_h
            px_w = bbox.size.width * img_w
            px_h = bbox.size.height * img_h

            # Convert pixel coords to screen points
            screen_x = window_x + int(px_x / scale_factor)
            screen_y = window_y + int(px_y / scale_factor)
            screen_w = int(px_w / scale_factor)
            screen_h = int(px_h / scale_factor)

            hints.append(OCRHint(
                text=text,
                x=screen_x,
                y=screen_y,
                width=screen_w,
                height=screen_h,
                confidence=round(conf, 3),
            ))

        return hints


class WindowsOCR(OCREngine):
    """Windows OCR via Windows.Media.Ocr."""

    def is_available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from winrt.windows.media.ocr import OcrEngine as _OcrEngine  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        # Windows OCR implementation would go here
        # Using winrt-Windows.Media.Ocr package
        logger.warning("Windows OCR not yet implemented")
        return []


class LinuxOCR(OCREngine):
    """Linux OCR via pytesseract."""

    def is_available(self) -> bool:
        if sys.platform == "darwin" or sys.platform == "win32":
            return False
        try:
            import pytesseract  # noqa: F401
            return True
        except ImportError:
            return False

    def recognize(self, image_data: bytes, scale_factor: float = 1.0,
                  window_x: int = 0, window_y: int = 0,
                  window_w: int = 0, window_h: int = 0) -> list[OCRHint]:
        import pytesseract
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_data))
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        hints = []
        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            if not text:
                continue
            conf = int(data["conf"][i])
            if conf < 30:  # Skip low-confidence detections
                continue

            px_x = data["left"][i]
            px_y = data["top"][i]
            px_w = data["width"][i]
            px_h = data["height"][i]

            screen_x = window_x + int(px_x / scale_factor)
            screen_y = window_y + int(px_y / scale_factor)
            screen_w = int(px_w / scale_factor)
            screen_h = int(px_h / scale_factor)

            hints.append(OCRHint(
                text=text,
                x=screen_x,
                y=screen_y,
                width=screen_w,
                height=screen_h,
                confidence=round(conf / 100.0, 3),
            ))

        return hints


def get_ocr_engine() -> OCREngine | None:
    """Get the best available OCR engine for the current platform."""
    if sys.platform == "darwin":
        engine = MacOSOCR()
        if engine.is_available():
            return engine
    elif sys.platform == "win32":
        engine = WindowsOCR()
        if engine.is_available():
            return engine
    else:
        engine = LinuxOCR()
        if engine.is_available():
            return engine
    return None
