"""Keep installation diagnostics available even when an optional dependency is broken."""
import sys

if __name__ == '__main__':
    if '--installation-self-test' in sys.argv[1:]:
        try:
            from comfybridge.install_platform import run_installation_self_test
            result = run_installation_self_test()
        except Exception:
            import json
            print(json.dumps({'ok': False, 'error': 'installation_self_test_failed'}))
            result = 1
        raise SystemExit(result)
    from comfybridge.launcher import main
    raise SystemExit(main())
