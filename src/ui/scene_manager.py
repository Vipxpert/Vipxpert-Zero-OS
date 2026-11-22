# /vipxpert_minimal_os/src/ui/scene_manager.py
import importlib
import os

SCENE_CATALOG = []
SCENE_LABELS = {}
SCENE_CATEGORIES = []
SCENE_GUIDES = {}
SLOT_CATEGORIES = {}
AMBIENT_PHYSICS_SCENES = set()
INTERACTIVE_SCENES = set()

_CAT_MAP = {
    "clock": ("cat_clocks", "Clocks"),
    "music": ("cat_music", "Music & Audio HUDs"),
    "saver": ("cat_savers", "Interactive Savers"),
    "scope": ("cat_scopes", "Hardware Scopes"),
}


class ScenePlugin:
    # Base class for modular Lock Screen scene plugins (Defines metadata, lifecycle hooks, input, and draw interface)
    SCENE_ID = ""
    TITLE = ""
    CATEGORY = "saver"
    ORDER = 100
    GUIDE_TEXT = "*:Unlock Screen   A/B:Slot|2/8:PiVol 7/9:BTV #:HUD"
    IS_INTERACTIVE = False
    IS_SAVER = False
    IS_PHYSICS = False

    def __init__(self, display, ui_manager, app_ref):
        # Scene Plugin Initializer (Stores display driver, UIManager, and parent LockScreenApp references)
        self.display = display
        self.ui = ui_manager
        self.app = app_ref

    @classmethod
    def should_auto_activate(cls, app_ref) -> bool:
        # Conditional Auto-Activation Hook (Override to return True when lock screen conditions match this scene)
        return False

    def handle_key(self, action: str, key: str):
        # Scene Key Input Hook (Returns True if consumed by scene, False to fall back to media keys)
        return False

    def draw(self):
        # Scene Frame Renderer (Draws the scene onto self.display)
        pass


class SceneManager:
    # Dynamic plugin loader and dispatcher for Lock Screen scenes (Discovers plugins, populates registries, and resolves overrides)
    _plugins_classes = {}
    _instances = {}

    @classmethod
    def discover(cls, scenes_pkg_path: str = "src.scenes"):
        # Plugin Discovery Crawler (Imports scene modules/bundles, sorts by ORDER, and populates global catalogs)
        SCENE_CATALOG.clear()
        SCENE_LABELS.clear()
        SCENE_CATEGORIES.clear()
        SCENE_GUIDES.clear()
        SLOT_CATEGORIES.clear()
        AMBIENT_PHYSICS_SCENES.clear()
        INTERACTIVE_SCENES.clear()
        cls._plugins_classes.clear()
        cls._instances.clear()

        cat_buckets = {k: [] for k in _CAT_MAP.keys()}
        try:
            mod = importlib.import_module(scenes_pkg_path)
        except ImportError:
            return

        pkg_dir = os.path.dirname(mod.__file__)
        for root, dirs, files in os.walk(pkg_dir):
            dirs.sort()
            for file in sorted(files):
                if file.endswith(".py") and not file.startswith("__"):
                    rel_path = os.path.relpath(os.path.join(root, file), pkg_dir)
                    mod_name = scenes_pkg_path + "." + rel_path[:-3].replace(os.sep, ".")
                    try:
                        imported = importlib.import_module(mod_name)
                        for attr_name in dir(imported):
                            obj = getattr(imported, attr_name)
                            if isinstance(obj, type) and issubclass(obj, ScenePlugin) and obj is not ScenePlugin:
                                sid = getattr(obj, "SCENE_ID", "")
                                if sid and obj.CATEGORY in cat_buckets:
                                    cls._plugins_classes[sid] = obj
                                    cat_buckets[obj.CATEGORY].append((getattr(obj, "ORDER", 100), sid))
                                    SCENE_LABELS[sid] = obj.TITLE
                                    SCENE_GUIDES[sid] = (obj.TITLE, obj.GUIDE_TEXT)
                                    if getattr(obj, "IS_PHYSICS", False):
                                        AMBIENT_PHYSICS_SCENES.add(sid)
                                    if getattr(obj, "IS_INTERACTIVE", False) or getattr(obj, "IS_PHYSICS", False):
                                        INTERACTIVE_SCENES.add(sid)
                    except Exception as e:
                        print(f"[SceneManager] Error importing {mod_name}: {e}")

        all_ids = []
        sorted_buckets = {}
        for cat_key, items in cat_buckets.items():
            items.sort(key=lambda x: (x[0], x[1]))
            sids = [sid for _, sid in items]
            sorted_buckets[cat_key] = sids
            for sid in sids:
                SCENE_CATALOG.append((sid, SCENE_LABELS[sid]))
                all_ids.append(sid)
            if sids:
                cat_id, cat_title = _CAT_MAP.get(cat_key, ("cat_other", "Other"))
                SCENE_CATEGORIES.append((cat_id, cat_title, list(sids)))

        SCENE_CATEGORIES.insert(0, ("cat_all", "All Scenes", all_ids))
        SLOT_CATEGORIES["clock"] = sorted_buckets.get("clock", []) + sorted_buckets.get("saver", []) + all_ids
        SLOT_CATEGORIES["music"] = sorted_buckets.get("music", []) + all_ids
        SLOT_CATEGORIES["ambient"] = sorted_buckets.get("music", []) + sorted_buckets.get("scope", []) + all_ids
        SLOT_CATEGORIES["amb_music"] = sorted_buckets.get("music", []) + all_ids
        SLOT_CATEGORIES["screensaver"] = sorted_buckets.get("saver", []) + sorted_buckets.get("scope", []) + all_ids

    @classmethod
    def get_instance(cls, scene_id: str, display, ui_manager, app_ref):
        # Singleton Scene Instance Resolver (Lazily instantiates and returns the plugin for scene_id)
        if not cls._plugins_classes:
            cls.discover()
        key = (id(app_ref), scene_id)
        if key not in cls._instances:
            p_cls = cls._plugins_classes.get(scene_id)
            if p_cls is None:
                p_cls = cls._plugins_classes.get("clock_digital")
            if p_cls is not None:
                cls._instances[key] = p_cls(display, ui_manager, app_ref)
            else:
                return None
        return cls._instances[key]

    @classmethod
    def resolve_auto_scene(cls, app_ref):
        # Condition-Based Override Evaluator (Checks registered plugins for active auto-trigger conditions)
        if not cls._plugins_classes:
            cls.discover()
        for sid, p_cls in cls._plugins_classes.items():
            try:
                if p_cls.should_auto_activate(app_ref):
                    return sid
            except Exception:
                pass
        return None
