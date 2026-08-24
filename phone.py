import socket
import threading
import sys

def get_local_ip():
    """Get the local IP address of this device."""
    # Method 1: Try UDP connect to a broadcast address (doesn't actually send packets)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass

    # Method 2: Linux/Android specific - use ioctl to get interface IP
    try:
        import fcntl
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Try common WiFi interface names
        for ifname in ['wlan0', 'wlan1', 'eth0', 'en0']:
            try:
                ip = socket.inet_ntoa(fcntl.ioctl(
                    s.fileno(),
                    0x8915,  # SIOCGIFADDR
                    struct.pack('256s', ifname[:15].encode('utf-8'))
                )[20:24])
                s.close()
                return ip
            except Exception:
                continue
        s.close()
    except ImportError:
        pass

    # Fallback
    return '127.0.0.1'

def receive_messages(client_socket):
    """Receive messages from the PC and display them."""
    while True:
        try:
            data = client_socket.recv(1024)
            if not data:
                print("\n[Connection lost] The PC has disconnected.")
                break
            message = data.decode('utf-8')
            # Print incoming message and re-show prompt
            print(f"\n[PC] {message}")
            print("[You] ", end="", flush=True)
        except ConnectionResetError:
            print("\n[Connection lost] Connection reset by PC.")
            break
        except Exception:
            print("\n[Connection lost] An error occurred.")
            break

def start_server():
    host = '0.0.0.0'
    port = 5000

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((host, port))
    except OSError:
        print(f"Error: Port {port} is already in use. Try a different port.")
        sys.exit(1)

    server.listen(1)
    local_ip = get_local_ip()

    print("=" * 40)
    print("       PHONE LAN CHAT SERVER")
    print("=" * 40)
    print(f"Local IP Address : {local_ip}")
    print(f"Port             : {port}")
    print("=" * 40)
    print("Waiting for PC to connect...")

    try:
        client_socket, addr = server.accept()
        print(f"\n[Connected] PC connected from {addr[0]}:{addr[1]}")
        print("Type your messages below.")
        print("Type 'exit' or 'quit' to disconnect.\n")

        # Start a thread to receive messages from the PC
        receive_thread = threading.Thread(target=receive_messages, args=(client_socket,))
        receive_thread.daemon = True
        receive_thread.start()

        # Main thread handles sending messages
        while True:
            try:
                msg = input("[You] ")
                if msg.lower() in ['exit', 'quit']:
                    print("Disconnecting...")
                    break
                if msg:
                    client_socket.send(msg.encode('utf-8'))
            except KeyboardInterrupt:
                print("\nDisconnecting...")
                break
            except Exception:
                print("\n[Error] Failed to send message.")
                break
    except KeyboardInterrupt:
        print("\nServer shutting down...")
    finally:
        try:
            client_socket.close()
        except Exception:
            pass
        server.close()
        print("Server stopped.")

if __name__ == "__main__":
    start_server()
