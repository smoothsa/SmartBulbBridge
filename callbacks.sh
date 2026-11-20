#!/bin/bash
for arg in "$@"; do
  case "$arg" in
    -l|--list) echo "c++:libfpp-co-SmartBulbBridge.so"; exit 0 ;;
    -h|--help|-v|--version) exit 0 ;;
    --) break ;;
    *) exit 1 ;;
  esac
done
exit 0
