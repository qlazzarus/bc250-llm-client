#!/usr/bin/python3
# This launcher is installed alongside the validated root-owned worker tree.
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, '/opt/bc250-model-control/lib')
from backend.app.model_control.cli import main
raise SystemExit(main())
