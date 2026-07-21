try:
    from flask import Flask
except ImportError:
    Flask = None

if Flask is not None:
    app = Flask(__name__)

    @app.route("/")
    def hello_world():
        return "<p>Hello, World!</p>"
else:
    import http.server
    import socketserver

    PORT = 5000

    class HelloHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<p>Hello, World!</p>")
            else:
                super().do_GET()

    def main():
        with socketserver.TCPServer(("0.0.0.0", PORT), HelloHandler) as httpd:
            print(f"Flask is not installed. Serving on port {PORT} using the built-in HTTP server.")
            httpd.serve_forever()

    if __name__ == "__main__":
        main()