import base64
import email.policy
from email.parser import BytesParser
from html.parser import HTMLParser

class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div", "li", "tr"):
            self.text.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div", "li", "tr"):
            self.text.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def parse_message(raw):
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    message = BytesParser(policy=email.policy.default).parsebytes(data)
    pieces = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        # File attachments (including inline named files) are outside this scanner.
        if part.get_filename():
            continue
        text = part.get_content()
        if part.get_content_type() == "text/html":
            parser = TextHTML()
            parser.feed(text)
            text = "".join(parser.text)
        pieces.append(text)
    return {
        "sender": str(message.get("From", "")),
        "subject": str(message.get("Subject", "")),
        "body": "\n".join(pieces),
    }


