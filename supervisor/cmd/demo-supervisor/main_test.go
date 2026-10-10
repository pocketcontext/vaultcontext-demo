package main

import (
	"fmt"
	"net"
	"path/filepath"
	"testing"
	"time"
)

func TestReadinessRequiresCommittedCurrentGeneration(t *testing.T) {
	for _, tc := range []struct {
		name, body string
		want       bool
	}{
		{"active", `{"ok":true,"result":{"ready":true,"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","generation":"2026-10-10"}}`, true},
		{"fenced", `{"ok":true,"result":{"ready":false,"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","generation":"2026-10-10"}}`, false},
		{"midnight", `{"ok":true,"result":{"ready":true,"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","generation":"2026-10-09"}}`, false},
		{"malformed", `{"ok":true}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			socket := filepath.Join(t.TempDir(), "control.sock")
			listener, err := net.Listen("unix", socket)
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			go func() {
				conn, err := listener.Accept()
				if err != nil {
					return
				}
				defer conn.Close()
				buf := make([]byte, 1024)
				conn.Read(buf)
				fmt.Fprintln(conn, tc.body)
			}()
			_, err = ready(socket, func() time.Time { return time.Date(2026, 10, 10, 0, 0, 0, 0, time.UTC) })
			if (err == nil) != tc.want {
				t.Fatalf("ready error=%v, want success=%v", err, tc.want)
			}
		})
	}
}
