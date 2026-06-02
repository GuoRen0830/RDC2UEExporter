import importlib
import qrenderdoc as qrd

def _log(msg):
    print("[RDC2UE]" + msg)

def export_current_draw_callback(ctx, data):
    """菜单按钮回调函数"""
    _log("Export Current Draw clicked")

    try:
        from . import rdc2ue_exporter
        importlib.reload(rdc2ue_exporter)

        result = rdc2ue_exporter.export_current_draw_from_plugin(ctx)
        if result is None:
            _log("Export failed")
            return
        
    except Exception as e:
        _log("Export failed: {}".format(e))

def register(version, ctx):
    _log("Register RDC2UE plugin for version {}".format(version))

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Window,
        ["RDC2UE", "Export Current Draw"],
        export_current_draw_callback
    )

def unregister():
    _log("Unregister RDC2UE plugin")
