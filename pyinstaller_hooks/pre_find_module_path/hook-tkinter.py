def pre_find_module_path(hook_api):
    # Keep tkinter discoverable even if PyInstaller's built-in Tcl/Tk probe
    # marks the host Python as broken. We bundle Tcl/Tk data manually in spec.
    return
