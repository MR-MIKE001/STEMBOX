"""Start the STEMBox server.

    python run.py                 # serves on 0.0.0.0:5000 (all network interfaces)
    STEMBOX_PORT=8080 python run.py

Uses the Waitress production server when it is installed (recommended), and
falls back to Flask's built-in threaded server otherwise.
"""
from app import create_app
from app.network import lan_addresses

app = create_app()


def main():
    host = app.config["HOST"]
    port = app.config["PORT"]
    print("=" * 60)
    print(" STEMBox is running.")
    print(f" On this computer:   http://127.0.0.1:{port}")
    for ip in lan_addresses():
        print(f" On the local Wi-Fi: http://{ip}:{port}")
    print(" Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        from waitress import serve
    except ImportError:
        app.run(host=host, port=port, threaded=True, debug=False)
    else:
        serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
