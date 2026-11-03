"""Compatibility entry point for the separated prepare/score pipeline.

The previous one-shot generation runner is preserved in the historical run
record package. No arguments prints help rather than starting paid calls.
"""
import sys
from eval_bridge.cli import main

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:] if len(sys.argv) > 1 else ['--help']))
