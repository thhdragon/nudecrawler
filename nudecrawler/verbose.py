import inspect
import os

verbose = False
# send_bugreports = False


def get_verbose():
    return verbose


def printv(*args):
    if not verbose:
        return

    if False:
        frame = inspect.stack()[1]
        location = os.path.basename(frame.filename) + ":" + str(frame.lineno)
        print("...", f"({location})", *args)
    else:
        print("...", *args)
