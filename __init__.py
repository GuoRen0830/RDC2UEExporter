import importlib
import qrenderdoc as qrd


def _log(msg):
    print("[RDC2UE]" + msg)


def export_draw_range_callback(ctx, data):
    _log("Export Draw Range clicked")

    try:
        from . import rdc2ue_exporter
        importlib.reload(rdc2ue_exporter)

        result = rdc2ue_exporter.export_from_plugin(ctx)
        if result is None:
            _log("Range export failed")
            return

    except Exception as e:
        _log("Range export failed: {}".format(e))


def register(version, ctx):
    _log("Register RDC2UE plugin")

    ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Window,
        ["RDC2UE", "Export Draw Range"],
        export_draw_range_callback
    )


def unregister():
    _log("Unregister RDC2UE plugin")
