// demo-supervisor owns container lifetime and externally visible readiness.
// The storage worker retains exclusive volume ownership and the database helpers.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"regexp"
	"sync/atomic"
	"syscall"
	"time"
)

func ready(socket string, clock func() time.Time) (string, error) {
	conn, err := net.DialTimeout("unix", socket, time.Second)
	if err != nil {
		return "", err
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err = fmt.Fprintln(conn, `{"command":"status","value":null}`); err != nil {
		return "", err
	}
	var response struct {
		OK     bool `json:"ok"`
		Result struct {
			Ready      bool   `json:"ready"`
			Revision   string `json:"revision"`
			Generation string `json:"generation"`
		} `json:"result"`
	}
	if err = json.NewDecoder(bufio.NewReader(conn)).Decode(&response); err != nil {
		return "", err
	}
	if !response.OK || !response.Result.Ready || response.Result.Generation != clock().UTC().Format("2006-01-02") || !regexp.MustCompile(`^[a-f0-9]{40}$`).MatchString(response.Result.Revision) {
		return "", errors.New("not ready")
	}
	return response.Result.Revision, nil
}

func run() error {
	var stopping atomic.Bool
	worker := exec.Command("/usr/bin/python3", "/usr/local/bin/vaultcontext-demo-once.py", "run")
	// Never forward private child output into container logs.
	worker.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	listener, err := net.Listen("tcp", "127.0.0.1:8782")
	if err != nil {
		return errors.New("readiness listener failed")
	}
	server := &http.Server{ReadHeaderTimeout: 2 * time.Second, WriteTimeout: 4 * time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if r.URL.Path != "/ready" || r.Method != "GET" {
			http.NotFound(w, r)
			return
		}
		revision, err := ready("/storage/control/control.sock", time.Now)
		if err != nil || stopping.Load() {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"ready": true, "revision": revision})
	})}
	defer server.Close()
	if err = worker.Start(); err != nil {
		listener.Close()
		return errors.New("storage worker failed to start")
	}
	serveErrors := make(chan error, 1)
	go func() { serveErrors <- server.Serve(listener) }()
	exited := make(chan error, 1)
	go func() { exited <- worker.Wait() }()
	select {
	case err = <-exited:
		stopping.Store(true)
		syscall.Kill(-worker.Process.Pid, syscall.SIGKILL)
		return errors.New("storage worker exited unexpectedly")
	case <-serveErrors:
		err = errors.New("readiness listener failed")
	case <-signals:
	}
	serveFailed := err != nil
	stopping.Store(true)
	worker.Process.Signal(syscall.SIGTERM)
	// Two sequential 360s database drains plus gate overhead fit inside 900s.
	timer := time.NewTimer(850 * time.Second)
	defer timer.Stop()
	select {
	case err = <-exited:
		syscall.Kill(-worker.Process.Pid, syscall.SIGKILL)
		if err != nil || serveFailed {
			return errors.New("storage worker drain failed")
		}
		return nil
	case <-timer.C:
		syscall.Kill(-worker.Process.Pid, syscall.SIGKILL)
		<-exited
		return errors.New("storage worker drain timed out")
	}
}
func main() {
	if run() != nil {
		fmt.Fprintln(os.Stderr, "demo supervisor failed; private operator review required")
		os.Exit(1)
	}
}
