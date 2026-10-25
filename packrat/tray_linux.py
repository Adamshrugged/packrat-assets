"""Linux tray icon over D-Bus (StatusNotifierItem, with a com.canonical.dbusmenu menu), the protocol KDE,
Ubuntu, XFCE and Cinnamon trays use. The older X11 tray protocol shows a blank icon on Wayland desktops."""
import os
import threading

from jeepney import DBusAddress, HeaderFields, MessageType, new_error, new_method_call, new_method_return
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

ITEM = "/StatusNotifierItem"
MENU = "/MenuBar"
WATCHER = DBusAddress("/StatusNotifierWatcher", bus_name="org.kde.StatusNotifierWatcher",
                      interface="org.kde.StatusNotifierWatcher")


def available() -> bool:
    """True when the desktop has a tray that accepts StatusNotifierItems."""
    try:
        with open_dbus_connection(bus="SESSION") as conn:
            reply = conn.send_and_get_reply(message_bus.NameHasOwner(WATCHER.bus_name), timeout=2)
            return bool(reply.body[0])
    except Exception:
        return False


def _pixmaps(image) -> list:
    """The icon as (width, height, ARGB32 big-endian bytes) at a few sizes."""
    out = []
    for size in (22, 32, 64):
        im = image.convert("RGBA").resize((size, size))
        argb = bytearray()
        for r, g, b, a in im.getdata():
            argb += bytes((a, r, g, b))
        out.append((size, size, bytes(argb)))
    return out


class Tray:
    """Run with run() (blocks); stop() from any thread. menu: [(label, callback)]; the first item is also what a
    left click on the icon does."""

    def __init__(self, title: str, image, menu: list[tuple[str, callable]]):
        self.title, self.menu = title, menu
        self.pixmaps = _pixmaps(image)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ D-Bus objects

    def _item_props(self) -> dict:
        return {
            "Category": ("s", "ApplicationStatus"), "Id": ("s", "packrat"), "Title": ("s", self.title),
            "Status": ("s", "Active"), "IconName": ("s", ""), "IconPixmap": ("a(iiay)", self.pixmaps),
            "ToolTip": ("(sa(iiay)ss)", ("", [], self.title, "")), "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU),
        }

    def _menu_props(self) -> dict:
        return {"Version": ("u", 3), "Status": ("s", "normal"), "TextDirection": ("s", "ltr")}

    def _layout(self):
        children = [("(ia{sv}av)", (i + 1, {"label": ("s", label)}, [])) for i, (label, _) in enumerate(self.menu)]
        return (1, (0, {"children-display": ("s", "submenu")}, children))

    def _handle(self, msg):
        path, iface, member = (msg.header.fields.get(f) for f in
                               (HeaderFields.path, HeaderFields.interface, HeaderFields.member))
        props = self._item_props() if path == ITEM else self._menu_props() if path == MENU else None
        if props is None:
            return new_error(msg, "org.freedesktop.DBus.Error.UnknownObject")
        if iface == "org.freedesktop.DBus.Properties":
            if member == "GetAll":
                return new_method_return(msg, "a{sv}", (props,))
            if member == "Get" and msg.body[1] in props:
                return new_method_return(msg, "v", (props[msg.body[1]],))
        elif member == "Introspect":
            return new_method_return(msg, "s", ("<node/>",))
        elif member == "Activate":
            self._run(0)
            return new_method_return(msg)
        elif member in ("SecondaryActivate", "ContextMenu", "Scroll"):
            return new_method_return(msg)
        elif member == "GetLayout":
            return new_method_return(msg, "u(ia{sv}av)", self._layout())
        elif member == "GetGroupProperties":
            items = [(i + 1, {"label": ("s", label)}) for i, (label, _) in enumerate(self.menu)]
            return new_method_return(msg, "a(ia{sv})", (items,))
        elif member == "Event":
            if msg.body[1] == "clicked":
                self._run(msg.body[0] - 1)
            return new_method_return(msg)
        elif member == "EventGroup":
            for item_id, event, *_ in msg.body[0]:
                if event == "clicked":
                    self._run(item_id - 1)
            return new_method_return(msg, "ai", ([],))
        elif member == "AboutToShow":
            return new_method_return(msg, "b", (False,))
        elif member == "AboutToShowGroup":
            return new_method_return(msg, "aiai", ([], []))
        return new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod")

    def _run(self, index: int) -> None:
        if 0 <= index < len(self.menu):
            threading.Thread(target=self.menu[index][1], daemon=True).start()

    # ------------------------------------------------------------ main loop

    def run(self) -> None:
        with open_dbus_connection(bus="SESSION") as conn:
            name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            conn.send_and_get_reply(message_bus.RequestName(name), timeout=5)
            conn.send_and_get_reply(new_method_call(WATCHER, "RegisterStatusNotifierItem", "s", (name,)), timeout=5)
            while not self._stop.is_set():
                try:
                    msg = conn.receive(timeout=0.5)
                except TimeoutError:
                    continue
                if msg.header.message_type == MessageType.method_call:
                    conn.send(self._handle(msg))
