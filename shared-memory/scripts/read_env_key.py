#!/usr/bin/env python3
"""Thin CLI wrapper around secure_env.read_env_value for shell scripts."""
import sys
from pathlib import Path

# Add script directory to sys.path so secure_env can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent))
from secure_env import read_env_value

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(0)
    val = read_env_value(sys.argv[1], sys.argv[2])
    if val is not None:
        sys.stdout.write(val)
