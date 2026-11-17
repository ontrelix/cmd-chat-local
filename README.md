## `HOW_TO_USE`

### 01. Requirements
- Python 3.8 or newer.
- Two or more devices connected to the same Wi-Fi or hotspot.
- CMD or Terminal.

### 02. Start the Host

```bash
python lan_chat.py host
```

### 03. Join the Room

```bash
python lan_chat.py join 192.168.1.20
```

Replace the IP with the host's actual IP address.

### 04. Chat Commands

| Command | Description |
|---|---|
| `/help` | Show help |
| `/nick NAME` | Change nickname |
| `/users` | List connected users |
| `/msg NAME TEXT` | Send a private message |
| `/me TEXT` | Send an action |
| `/info` | Connection information |
| `/clear` | Clear terminal |
| `/quit` | Leave the chat |

### 05. Android

Run `lan_chat.py` using Pydroid 3 or Termux and connect to the host's IP address.

## `IMPORTANT_NOTES`

- All devices must be on the same local network.
- Allow Python through Windows Firewall if requested.
- Messages are not encrypted.
- Chat history is not saved.
- Designed for local network communication.

---

<text align="center">DEVELOPED BY ONTRELIX</text>

<text align="center">`PYTHON // LAN // TERMINAL // CHAT`</text>
