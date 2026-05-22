import http.server
import socketserver
import urllib.request
import json
import sys

PORT = 8888

class DiagnosticProxy(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        print(f"\n--- INCOMING REQUEST TO PROXY ---")
        print(f"Path: {self.path}")
        
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        print(f"Body: {post_data.decode('utf-8', errors='ignore')[:200]}...")

        # Determine target
        if "/embeddings" in self.path:
            target = "http://localhost:8070" + self.path
        else:
            target = "http://localhost:5000" + self.path
            
        print(f"Routing to: {target}")
        
        try:
            headers = dict(self.headers)
            if 'Host' in headers: del headers['Host']
            
            req = urllib.request.Request(target, data=post_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as response:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() == 'transfer-encoding': continue
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(response.read())
                print(f"Response Sent: {response.status}")
        except Exception as e:
            print(f"Proxy Error: {str(e)}")
            try:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            except:
                pass

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    with ThreadingHTTPServer(("", PORT), DiagnosticProxy) as httpd:
        print(f"Diagnostic Threaded Proxy running on port {PORT}")
        httpd.serve_forever()
