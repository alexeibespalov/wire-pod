package wirepod_vosk

// Optional Whisper (or any Wyoming ASR server) on top of Vosk.
//
// WYOMING_STT=host:port sends each finished utterance (16 kHz mono s16le PCM) to a Wyoming
// speech-to-text server, e.g. rhasspy/wyoming-whisper, and uses its transcript. Vosk keeps
// running on the stream as before, so its result is the instant fallback when the server
// is down, slow (WYOMING_STT_TIMEOUT seconds, default 8) or returns nothing.
//
// Wyoming protocol: each event is a JSON header line {"type", "data_length",
// "payload_length"} followed by data_length bytes of JSON data and payload_length bytes.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type wyEvent struct {
	Type          string          `json:"type"`
	Data          json.RawMessage `json:"data,omitempty"`
	DataLength    int             `json:"data_length,omitempty"`
	PayloadLength int             `json:"payload_length,omitempty"`
}

func wySend(w io.Writer, typ string, data any, payload []byte) error {
	body, _ := json.Marshal(data)
	hdr, _ := json.Marshal(map[string]any{"type": typ, "data_length": len(body), "payload_length": len(payload)})
	buf := append(append(append(hdr, '\n'), body...), payload...)
	_, err := w.Write(buf)
	return err
}

func wyRecv(r *bufio.Reader) (string, map[string]any, error) {
	line, err := r.ReadBytes('\n')
	if err != nil {
		return "", nil, err
	}
	var ev wyEvent
	if err := json.Unmarshal(line, &ev); err != nil {
		return "", nil, err
	}
	data := map[string]any{}
	if len(ev.Data) > 0 {
		_ = json.Unmarshal(ev.Data, &data)
	}
	if ev.DataLength > 0 {
		b := make([]byte, ev.DataLength)
		if _, err := io.ReadFull(r, b); err != nil {
			return "", nil, err
		}
		_ = json.Unmarshal(b, &data)
	}
	if ev.PayloadLength > 0 {
		if _, err := io.CopyN(io.Discard, r, int64(ev.PayloadLength)); err != nil {
			return "", nil, err
		}
	}
	return ev.Type, data, nil
}

func wyomingTranscribe(addr string, pcm []byte, timeout time.Duration) (string, error) {
	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		return "", err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	w := bufio.NewWriter(conn)
	format := map[string]any{"rate": 16000, "width": 2, "channels": 1}
	if err := wySend(w, "transcribe", map[string]any{"language": "en"}, nil); err != nil {
		return "", err
	}
	if err := wySend(w, "audio-start", format, nil); err != nil {
		return "", err
	}
	for i := 0; i < len(pcm); i += 3200 { // 100 ms chunks
		end := i + 3200
		if end > len(pcm) {
			end = len(pcm)
		}
		if err := wySend(w, "audio-chunk", format, pcm[i:end]); err != nil {
			return "", err
		}
	}
	if err := wySend(w, "audio-stop", map[string]any{}, nil); err != nil {
		return "", err
	}
	if err := w.Flush(); err != nil {
		return "", err
	}
	r := bufio.NewReader(conn)
	for {
		typ, data, err := wyRecv(r)
		if err != nil {
			return "", err
		}
		if typ == "transcript" {
			text, _ := data["text"].(string)
			return strings.TrimSpace(text), nil
		}
	}
}

// Whisper invents these on silence or noise.
var whisperGhost = regexp.MustCompile(`(?i)^\W*(thank you( (so much|for watching))?|thanks( for watching)?|you|bye|okay|oh|um+|uh+|\.+)\W*$`)

// betterTranscript returns the Wyoming transcript when configured and usable, else Vosk's.
func betterTranscript(device string, pcm []byte, voskText string) string {
	addr := os.Getenv("WYOMING_STT")
	if addr == "" || len(pcm) == 0 {
		return voskText
	}
	timeout := 8 * time.Second
	if v, err := strconv.ParseFloat(os.Getenv("WYOMING_STT_TIMEOUT"), 64); err == nil && v > 0 {
		timeout = time.Duration(v * float64(time.Second))
	}
	t0 := time.Now()
	text, err := wyomingTranscribe(addr, pcm, timeout)
	took := time.Since(t0).Seconds()
	switch {
	case err != nil:
		fmt.Printf("(Bot %s) Whisper failed after %.1fs (%v); using Vosk\n", device, took, err)
		return voskText
	case strings.TrimSpace(voskText) == "" && whisperGhost.MatchString(text):
		fmt.Printf("(Bot %s) Whisper heard %q in silence; ignoring\n", device, text)
		return ""
	case text == "":
		return voskText
	}
	fmt.Printf("(Bot %s) Whisper (%.1fs): %q | Vosk: %q\n", device, took, text, voskText)
	return text
}
