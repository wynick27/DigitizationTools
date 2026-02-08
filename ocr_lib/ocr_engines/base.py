
class BaseOCREngine:
    def __init__(self, config=None):
        """
        Initialize the OCR engine.
        :param config: Dictionary containing engine-specific configuration.
        """
        self.config = config or {}

    def process_image(self, image_bytes):
        """
        Process an image and return OCR results.
        :param image_bytes: Raw bytes of the image file.
        :return: List of results (dictionaries or standardized format).
        """
        raise NotImplementedError("Subclasses must implement process_image")
