import socket
import threading
import sys

def receive_messages(client_socket):
    """Receive messages from the phone and display them."""
    while True:
        try:
            data = client_socket.recv(1024)
            if not data:
                print("\n[Connection lost] The phone has disconnected.")
                break
            message = data.decode('utf-8')
            # Print incoming message and re-show prompt
            print(f"\n[Phone] {message}")
            print("[You] ", end="", flush=True)
        except ConnectionResetError:
            print("\n[Connection lost] Connection reset by phone.")
            break
        except Exception:
            print("\n[Connection lost] An error occurred.")
            break

def start_client():
    print("=" * 40)
    print("       PC LAN CHAT CLIENT")
    print("=" * 40)

    # Get and validate IP address
    while True:
        ip = input("Enter phone IP address: ").strip()
        if not ip:
            print("Please enter a valid IP address.")
            continue
        break

    # Get and validate port number
    while True:
        port_str = input("Enter phone port: ").strip()
        try:
            port = int(port_str)
            if port < 1 or port > 65535:
                print("Please enter a valid port number (1-65535).")
                continue
            break
        except ValueError:
            print("Please enter a valid integer for the port.")

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.settimeout(10)

    print(f"\nConnecting to {ip}:{port}...")

    try:
        client_socket.connect((ip, port))
    except socket.timeout:
        print("Error: Connection timed out. The phone may not be reachable.")
        sys.exit(1)
    except socket.gaierror:
        print("Error: Invalid IP address or hostname.")
        sys.exit(1)
    except ConnectionRefusedError:
        print("Error: Connection refused. Make sure the phone server is running.")
        sys.exit(1)
    except OSError as e:
        print(f"Error: Could not connect to {ip}:{port} - {e}")
        sys.exit(1)

    client_socket.settimeout(None)
    print(f"[Connected] Successfully connected to phone at {ip}:{port}")
    print("Type your messages below.")
    print("Type 'exit' or 'quit' to disconnect.\n")

    # Start a thread to receive messages from the phone
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

    try:
        client_socket.close()
    except Exception:
        pass
    print("Disconnected.")

if __name__ == "__main__":
    start_client()
