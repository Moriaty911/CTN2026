"""Standard-library tests for the CTN modules."""
import os

# Most postflight tests exercise the JEV layer; production keeps it off (CTN_POSTFLIGHT_JEV unset).
# tests/test_postflight_switch.py covers the off state explicitly.
os.environ.setdefault('CTN_POSTFLIGHT_JEV', 'true')
