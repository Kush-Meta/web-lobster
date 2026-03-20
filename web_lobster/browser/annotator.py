"""Screenshot annotator — overlays numbered labels on interactive elements.

Takes a raw screenshot and the list of extracted elements, draws small
numbered badges next to each interactive element so the vision model
can reference them by number (e.g., "click element [14]").
"""

from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from web_lobster.core.schemas import PageElement
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

# Colors for annotation badges (cycling through for visibility)
BADGE_COLORS = [
    (220, 50, 50),    # red
    (50, 120, 220),   # blue
    (30, 160, 70),    # green
    (200, 120, 20),   # orange
    (140, 50, 180),   # purple
    (20, 160, 160),   # teal
]


class Annotator:
    """Overlays numbered labels on screenshots at element positions."""

    def __init__(self, badge_size: int = 20, font_size: int = 12):
        self.badge_size = badge_size
        self.font_size = font_size
        self._font: Optional[ImageFont.FreeTypeFont] = None

    def _get_font(self) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if self._font is None:
            try:
                # Try system fonts
                for font_path in [
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                    "/System/Library/Fonts/Helvetica.ttc",
                ]:
                    try:
                        self._font = ImageFont.truetype(font_path, self.font_size)
                        break
                    except (OSError, IOError):
                        continue
            except Exception:
                pass
            if self._font is None:
                self._font = ImageFont.load_default()
        return self._font

    def annotate(
        self,
        screenshot_bytes: bytes,
        elements: list[PageElement],
    ) -> bytes:
        """Draw numbered badges on the screenshot at element positions.

        Args:
            screenshot_bytes: Raw PNG/JPEG screenshot bytes
            elements: Elements with bounding boxes to annotate

        Returns:
            Annotated screenshot as JPEG bytes
        """
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        font = self._get_font()

        for i, element in enumerate(elements):
            if not element.bbox:
                continue

            color = BADGE_COLORS[i % len(BADGE_COLORS)]
            x = int(element.bbox.x)
            y = int(element.bbox.y)

            # Draw a subtle highlight around the element
            draw.rectangle(
                [x, y, x + int(element.bbox.width), y + int(element.bbox.height)],
                outline=color + (180,),
                width=2,
            )

            # Draw the numbered badge (top-left corner of element)
            badge_x = max(0, x - 2)
            badge_y = max(0, y - self.badge_size - 2)
            label = str(element.id)

            # Badge background
            text_bbox = font.getbbox(label)
            text_w = text_bbox[2] - text_bbox[0] + 8
            text_h = text_bbox[3] - text_bbox[1] + 4

            draw.rounded_rectangle(
                [badge_x, badge_y, badge_x + max(text_w, self.badge_size), badge_y + text_h + 2],
                radius=4,
                fill=color + (220,),
            )

            # Badge text
            draw.text(
                (badge_x + 4, badge_y + 1),
                label,
                fill=(255, 255, 255),
                font=font,
            )

        # Save as JPEG
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=85)
        return output.getvalue()
