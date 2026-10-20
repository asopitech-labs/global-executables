package registryinspect

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// freezingProxy forwards TCP connections until freeze is called; from then on the
// connections it already has go silent (bytes are swallowed, nothing is closed), like a
// connection whose NAT entry vanished while the machine was suspended. New connections work.
type freezingProxy struct {
	listener    net.Listener
	target      string
	connections atomic.Int32
	mu          sync.Mutex
	frozen      []*atomic.Bool
}

func newFreezingProxy(t *testing.T, target string) *freezingProxy {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	proxy := &freezingProxy{listener: listener, target: target}
	t.Cleanup(func() { _ = listener.Close() })
	go proxy.serve()
	return proxy
}

func (p *freezingProxy) serve() {
	for {
		client, err := p.listener.Accept()
		if err != nil {
			return
		}
		server, err := net.Dial("tcp", p.target)
		if err != nil {
			_ = client.Close()
			continue
		}
		p.connections.Add(1)
		frozen := &atomic.Bool{}
		p.mu.Lock()
		p.frozen = append(p.frozen, frozen)
		p.mu.Unlock()
		pipe := func(dst, src net.Conn) {
			buffer := make([]byte, 32*1024)
			for {
				n, err := src.Read(buffer)
				if n > 0 && !frozen.Load() {
					if _, err := dst.Write(buffer[:n]); err != nil {
						return
					}
				}
				if err != nil {
					return
				}
			}
		}
		go pipe(server, client)
		go pipe(client, server)
	}
}

func (p *freezingProxy) freeze() {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, frozen := range p.frozen {
		frozen.Store(true)
	}
}

func TestASilentHTTP2ConnectionIsDroppedSoRequestsFailOverToANewOne(t *testing.T) {
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.ProtoMajor != 2 {
			t.Errorf("want HTTP/2, got %s", r.Proto)
		}
		_, _ = io.WriteString(w, "ok")
	}))
	server.EnableHTTP2 = true
	server.StartTLS()
	defer server.Close()
	proxy := newFreezingProxy(t, server.Listener.Addr().String())

	config := Config{RequestTimeout: time.Second, MaxAttempts: 1, HealthCheckInterval: 200 * time.Millisecond,
		PingTimeout: 200 * time.Millisecond, UserAgent: "test"}
	defaults := newRequester(config).config
	transport := newTransport(defaults)
	transport.TLSClientConfig = server.Client().Transport.(*http.Transport).TLSClientConfig.Clone()
	config.Client = &http.Client{Transport: transport}
	requester := newRequester(config)
	target := "https://" + proxy.listener.Addr().String() + "/x"

	if response, err := requester.request(context.Background(), http.MethodGet, target, nil, 1024); err != nil || string(response.body) != "ok" {
		t.Fatalf("first request: %q %v", response.body, err)
	}
	proxy.freeze()
	if _, err := requester.request(context.Background(), http.MethodGet, target, nil, 1024); err == nil {
		t.Fatal("a request over the silent connection cannot succeed")
	}
	// The health check has dropped the silent connection, so the next request dials anew.
	deadline := time.Now().Add(5 * time.Second)
	for {
		response, err := requester.request(context.Background(), http.MethodGet, target, nil, 1024)
		if err == nil && string(response.body) == "ok" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("requests never left the silent connection: %v (connections %d)", err, proxy.connections.Load())
		}
	}
	if proxy.connections.Load() < 2 {
		t.Fatalf("connections = %d, want a new one", proxy.connections.Load())
	}
}
