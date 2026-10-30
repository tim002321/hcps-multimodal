import sys, traceback
sys.path.insert(0, r"F:\hcpsproject\PyCharmMiscProject\AAAwindows_code")

print("=== preflight ===")
try:
    from core.preflight import check_dependencies
    check_dependencies(need_ui=False)
    print("preflight OK")
except Exception:
    traceback.print_exc()

print()
print("=== import main ===")
try:
    import main
    print("main import OK")
except Exception:
    traceback.print_exc()

print()
print("=== import devices ===")
for m in ["devices.ecg_receiver", "devices.bracelet_receiver", "devices.camera_receiver",
          "devices.emg_receiver", "devices.eye_receiver"]:
    try:
        __import__(m)
        print(m, "OK")
    except Exception as e:
        print(m, "FAIL ->", type(e).__name__, e)