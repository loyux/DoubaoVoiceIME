"""Send a paste shortcut with a complete XKB map via zwp_virtual_keyboard_v1."""

import ctypes as c
import json
import os
import subprocess
import time


class Interface(c.Structure):
    pass


InterfacePointer = c.POINTER(Interface)


class Message(c.Structure):
    _fields_ = [("name", c.c_char_p), ("signature", c.c_char_p),
                ("types", c.POINTER(InterfacePointer))]


Interface._fields_ = [("name", c.c_char_p), ("version", c.c_int),
                      ("method_count", c.c_int), ("methods", c.POINTER(Message)),
                      ("event_count", c.c_int), ("events", c.POINTER(Message))]


class XkbNames(c.Structure):
    _fields_ = [(name, c.c_char_p) for name in ("rules", "model", "layout", "variant", "options")]


def bind(library, name, restype, *argtypes):
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


def keymap(shift):
    xkb = c.CDLL("libxkbcommon.so.0")
    context_new = bind(xkb, "xkb_context_new", c.c_void_p, c.c_int)
    context_unref = bind(xkb, "xkb_context_unref", None, c.c_void_p)
    map_new = bind(xkb, "xkb_keymap_new_from_names", c.c_void_p, c.c_void_p,
                   c.POINTER(XkbNames), c.c_int)
    map_unref = bind(xkb, "xkb_keymap_unref", None, c.c_void_p)
    map_string = bind(xkb, "xkb_keymap_get_as_string", c.c_void_p, c.c_void_p, c.c_int)
    mod_index = bind(xkb, "xkb_keymap_mod_get_index", c.c_uint, c.c_void_p, c.c_char_p)
    free = bind(c.CDLL(None), "free", None, c.c_void_p)
    context = context_new(0)
    if not context:
        raise RuntimeError("无法创建 XKB 上下文")
    try:
        names = XkbNames(b"evdev", b"pc105", b"us", b"", b"")
        mapping = map_new(context, c.byref(names), 0)
        if not mapping:
            raise RuntimeError("无法生成粘贴快捷键的 XKB 映射")
        try:
            pointer = map_string(mapping, 1)
            if not pointer:
                raise RuntimeError("无法导出 XKB 映射")
            try:
                modifiers = 1 << mod_index(mapping, b"Control")
                if shift:
                    modifiers |= 1 << mod_index(mapping, b"Shift")
                return c.string_at(pointer) + b"\0", c.c_uint(modifiers)
            finally:
                free(pointer)
        finally:
            map_unref(mapping)
    finally:
        context_unref(context)


def active_window(env=None):
    env = os.environ if env is None else env
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        output = subprocess.check_output(["hyprctl", "-j", "activewindow"], env=env,
                                         stderr=subprocess.DEVNULL, timeout=1)
        address = json.loads(output).get("address")
        return ("hyprland", address) if address and address != "0x0" else None
    if env.get("SWAYSOCK"):
        output = subprocess.check_output(["swaymsg", "-rt", "get_tree"], env=env,
                                         stderr=subprocess.DEVNULL, timeout=1)
        nodes = [json.loads(output)]
        while nodes:
            node = nodes.pop()
            if node.get("focused") and node.get("type") in ("con", "floating_con"):
                return ("sway", str(node["id"]))
            nodes.extend(node.get("nodes", []))
            nodes.extend(node.get("floating_nodes", []))
    return None


def paste(expected, terminal):
    lib = c.CDLL("libwayland-client.so.0")
    connect = bind(lib, "wl_display_connect", c.c_void_p, c.c_char_p)
    disconnect = bind(lib, "wl_display_disconnect", None, c.c_void_p)
    roundtrip = bind(lib, "wl_display_roundtrip", c.c_int, c.c_void_p)
    marshal = bind(lib, "wl_proxy_marshal_flags", c.c_void_p, c.c_void_p, c.c_uint,
                   InterfacePointer, c.c_uint, c.c_uint)
    listen = bind(lib, "wl_proxy_add_listener", c.c_int, c.c_void_p, c.c_void_p, c.c_void_p)
    registry_type = Interface.in_dll(lib, "wl_registry_interface")
    seat_type = Interface.in_dll(lib, "wl_seat_interface")

    keyboard_requests = (Message * 4)(
        Message(b"keymap", b"uhu"), Message(b"key", b"uuu"),
        Message(b"modifiers", b"uuuu"), Message(b"destroy", b""),
    )
    keyboard_type = Interface(b"zwp_virtual_keyboard_v1", 1, 4, keyboard_requests, 0, None)
    argument_types = (InterfacePointer * 2)(c.pointer(seat_type), c.pointer(keyboard_type))
    manager_requests = (Message * 1)(
        Message(b"create_virtual_keyboard", b"on", argument_types),
    )
    manager_type = Interface(b"zwp_virtual_keyboard_manager_v1", 1, 1, manager_requests, 0, None)
    globals_found = {}
    on_global = c.CFUNCTYPE(None, c.c_void_p, c.c_void_p, c.c_uint, c.c_char_p, c.c_uint)
    on_remove = c.CFUNCTYPE(None, c.c_void_p, c.c_void_p, c.c_uint)

    @on_global
    def register(_data, _registry, name, interface, _version):
        globals_found.setdefault(interface, name)

    @on_remove
    def unregister(_data, _registry, _name):
        pass

    callbacks = (c.c_void_p * 2)(c.cast(register, c.c_void_p), c.cast(unregister, c.c_void_p))
    display = connect(None)
    if not display:
        raise RuntimeError("无法连接 Wayland 显示服务器")
    try:
        registry = marshal(display, 1, c.byref(registry_type), 1, 0, None)
        if not registry or listen(registry, callbacks, None) < 0 or roundtrip(display) < 0:
            raise RuntimeError("无法读取 Wayland 接口")
        if manager_type.name not in globals_found or seat_type.name not in globals_found:
            raise RuntimeError("当前 Wayland 合成器未提供虚拟键盘接口")

        def get_global(interface):
            return marshal(registry, 0, c.byref(interface), 1, 0,
                           c.c_uint(globals_found[interface.name]), c.c_char_p(interface.name),
                           c.c_uint(1), None)

        seat, manager = get_global(seat_type), get_global(manager_type)
        if not seat or not manager:
            raise RuntimeError("无法绑定 Wayland 键盘接口")
        keyboard = marshal(manager, 0, c.byref(keyboard_type), 1, 0, c.c_void_p(seat), None)
        if not keyboard:
            raise RuntimeError("无法创建 Wayland 虚拟键盘")
        mapping, modifiers = keymap(terminal)
        fd = os.memfd_create("doubao-voice-xkb", os.MFD_CLOEXEC)
        try:
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(mapping)
            marshal(keyboard, 0, None, 1, 0, c.c_uint(1), c.c_int(fd), c.c_uint(len(mapping)))
            if roundtrip(display) < 0:
                raise RuntimeError("发送 Wayland 键盘映射失败")
            # Let the focused client process the new keymap before posting the shortcut.
            time.sleep(0.06)
            if active_window() != expected:
                return False
            marshal(keyboard, 2, None, 1, 0, modifiers, 0, 0, 0)
            now = c.c_uint(time.monotonic_ns() // 1_000_000)
            # Standard evdev KEY_V. XKB offsets evdev codes by 8.
            marshal(keyboard, 1, None, 1, 0, now, c.c_uint(47), c.c_uint(1))
            marshal(keyboard, 1, None, 1, 0, now, c.c_uint(47), c.c_uint(0))
            marshal(keyboard, 2, None, 1, 0, 0, 0, 0, 0)
            if roundtrip(display) < 0:
                raise RuntimeError("发送 Wayland 粘贴快捷键失败")
            time.sleep(0.08)
            marshal(keyboard, 3, None, 1, 1)
            return True
        finally:
            os.close(fd)
    finally:
        disconnect(display)


if __name__ == "__main__":
    import sys

    try:
        kind, window, shortcut = sys.argv[1:]
        sys.exit(0 if paste((kind, window), shortcut == "terminal") else 3)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
