import logging
import copy
from rich.logging import RichHandler
from rich.text import Text

# --- CONFIGURATION ---
ENABLE_FILE_LOGGING = True 
LOG_FILENAME = "application.log"

class DeMarkupFormatter(logging.Formatter):
    """
    Custom Formatter that strips Rich markup tags (e.g. [bold red]) 
    before writing to the log file.
    """
    def format(self, record):
        # 1. Create a shallow copy of the record so we don't modify 
        #    the original message that the Console/RichHandler needs.
        record_copy = copy.copy(record)
        
        # 2. If the message is a string, strip the markup
        if isinstance(record_copy.msg, str):
            # Text.from_markup parses the tags, .plain returns clean text
            record_copy.msg = Text.from_markup(record_copy.msg).plain
            
        # 3. Format the record using the standard logging formatter logic
        return super().format(record_copy)

def setup_logger(name="meraki_toolkit"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Prevent adding multiple handlers if the module is re-imported
    if not logger.handlers:
        # --- 1. Console Handler (Rich) ---
        # Keeps the tags and colors
        console_handler = RichHandler(rich_tracebacks=True, markup=True)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)

        # --- 2. File Handler (Plain Text) ---
        if ENABLE_FILE_LOGGING:
            file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
            file_handler.setLevel(logging.INFO)
            
            # Use our Custom Formatter here
            # This format string includes the timestamp, level, and message
            fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            file_formatter = DeMarkupFormatter(fmt)
            
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)

    return logger

# Initialize
logger = setup_logger()