"""An SMTP server that keeps what it is handed: where a test Stalwart's mail leaves to."""

import re
import socketserver
import threading
from dataclasses import dataclass

FINAL_RECIPIENT = re.compile(r"^Final-Recipient:\s*rfc822;\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class Relayed:
    sender: str  # empty for a bounce notice
    recipients: list[str]
    data: str


class MailSink:
    """The far end of the only route off the server: an SMTP server that keeps what it is handed.

    It refuses recipients whose local part starts with ``reject``, which is how a test provokes
    a bounce.
    """

    def __init__(self) -> None:
        self.messages: list[Relayed] = []
        self.refused: list[str] = []
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SinkSession)
        self.server.daemon_threads = True
        self.server.sink = self
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def clear(self) -> None:
        self.messages.clear()
        self.refused.clear()

    def relayed(self) -> set[str]:
        """Recipients of the mail handed over, bounce notices aside."""

        return {recipient for m in self.messages if m.sender for recipient in m.recipients}

    def bounced(self) -> set[str]:
        """The addresses that the bounce notices handed over report as failed."""

        return {a.lower() for m in self.messages if not m.sender for a in FINAL_RECIPIENT.findall(m.data)}


class SinkSession(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        sink: MailSink = self.server.sink
        sender, recipients = "", []
        self.reply("220 sink ESMTP")
        for line in self.rfile:
            command = line.decode(errors="replace").strip()
            verb = command.upper()
            if verb.startswith("MAIL FROM:"):
                sender, recipients = address_of(command), []
                self.reply("250 OK")
            elif verb.startswith("RCPT TO:") and address_of(command).startswith("reject"):
                sink.refused.append(address_of(command))
                self.reply("550 5.1.1 No such user")
            elif verb.startswith("RCPT TO:"):
                recipients.append(address_of(command))
                self.reply("250 OK")
            elif verb == "DATA":
                self.reply("354 Go ahead")
                sink.messages.append(Relayed(sender, recipients, self.read_data()))
                self.reply("250 OK")
            elif verb == "QUIT":
                self.reply("221 Bye")
                return
            else:
                self.reply("250 OK")  # EHLO, RSET, NOOP

    def read_data(self) -> str:
        lines = []
        for line in self.rfile:
            if line == b".\r\n":
                break
            lines.append(line.decode(errors="replace"))
        return "".join(lines)

    def reply(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")


def address_of(command: str) -> str:
    """The address of a MAIL FROM or RCPT TO command, lowercased; parameters after it are dropped."""

    return command.partition(":")[2].partition(">")[0].strip(" <").lower()
