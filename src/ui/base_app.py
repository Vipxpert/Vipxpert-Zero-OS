# /vipxpert_minimal_os/src/ui/base_app.py
from abc import ABC, abstractmethod
from src.ui.layout import LayoutEngine

class BaseApp(ABC):
    def _draw_state(self):
        """Default compositor hook to paint the layout to the active RAM canvas."""
        if hasattr(self, 'layout') and self.layout:
            self.layout.render()

    """
    Abstract Base Class representing a dynamic system application.
    All OS interface layers must implement this lifecycle interface.
    """
    # Shared library of standard safe UI sort configurations (supports both dict items and tuple/string items)
    STANDARD_TEXT_SORTS = {
        "A-Z": (lambda x: (x.get('name', '') if isinstance(x, dict) else str(x[0])).lower(), False),
        "Z-A": (lambda x: (x.get('name', '') if isinstance(x, dict) else str(x[0])).lower(), True)
    }

    def __init__(self, display):
        self.display = display  # Shared reference to the SH1106 OLED display
        self.layout = LayoutEngine(display)  # All-in-one declarative layout engine

    @abstractmethod
    async def on_start(self):
        """Triggered exactly once when the application shifts into the active foreground."""
        pass

    @abstractmethod
    async def on_stop(self):
        """Triggered exactly once right before the application is closed or swapped out."""
        pass

    @abstractmethod
    async def update(self):
        """Periodic UI frame execution loop (for updating state views or UI renders)."""
        pass

    @abstractmethod
    async def handle_key(self, action: str, key: str):
        """Targeted input routing block triggered whenever a keypad press occurs while focused."""
        pass

