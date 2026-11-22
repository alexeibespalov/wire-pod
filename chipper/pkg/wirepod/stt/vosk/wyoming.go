package wirepod_vosk

// Optional Whisper on top of Vosk. Each finished utterance (16 kHz mono s16le PCM) goes to
// a Whisper server and its transcript is used; Vosk keeps running on the stream as before,
// so its result is the instant fallback when the server is down, slow (WHISPER_TIMEOUT /
// WYOMING_STT_TIMEOUT seconds, default 8) or returns nothing.
//
//   WHISPER_HTTP=http://127.0.0.1:10330   whisper.cpp's whisper-server (/inference)
//   WYOMING_STT=host:port                 any Wyoming ASR server (e.g. rhasspy/wyoming-whisper)
//
// Wyoming protocol: each event is a JSON header line {"type", "data_length",
// "payload_length"} followed by data_length bytes of JSON data and payload_length bytes.

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
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

func wavBytes(pcm []byte) []byte {
	if pad := 32000 - len(pcm); pad > 0 { // whisper.cpp wants at least 1 s
		pcm = append(append([]byte{}, pcm...), make([]byte, pad)...)
	}
	var b bytes.Buffer
	w := func(v any) { _ = binary.Write(&b, binary.LittleEndian, v) }
	b.WriteString("RIFF")
	w(uint32(36 + len(pcm)))
	b.WriteString("WAVEfmt ")
	w(uint32(16))
	w(uint16(1))     // PCM
	w(uint16(1))     // mono
	w(uint32(16000)) // rate
	w(uint32(32000)) // byte rate
	w(uint16(2))     // block align
	w(uint16(16))    // bits
	b.WriteString("data")
	w(uint32(len(pcm)))
	b.Write(pcm)
	return b.Bytes()
}

func whisperHTTPTranscribe(base string, pcm []byte, timeout time.Duration) (string, error) {
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	fw, _ := mw.CreateFormFile("file", "audio.wav")
	_, _ = fw.Write(wavBytes(pcm))
	_ = mw.WriteField("response_format", "json")
	_ = mw.WriteField("temperature", "0")
	_ = mw.Close()
	client := &http.Client{Timeout: timeout}
	resp, err := client.Post(strings.TrimRight(base, "/")+"/inference", mw.FormDataContentType(), &body)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("whisper server: %s", resp.Status)
	}
	var out struct {
		Text string `json:"text"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", err
	}
	return strings.Join(strings.Fields(out.Text), " "), nil
}

// Whisper invents text on silence or noise: stock phrases, [BLANK_AUDIO], or a short
// made-up sentence ("What's the tape on?").
var whisperGhost = regexp.MustCompile(`(?i)^\W*(thank you( (so much|for watching))?|thanks( for watching)?|you|bye|okay|oh|um+|uh+|\.+)\W*$`)
var whisperTag = regexp.MustCompile(`\[[A-Z_ ]+\]|\([a-z ]+\)`) // [BLANK_AUDIO], (music)

func timeoutFromEnv(names ...string) time.Duration {
	for _, n := range names {
		if v, err := strconv.ParseFloat(os.Getenv(n), 64); err == nil && v > 0 {
			return time.Duration(v * float64(time.Second))
		}
	}
	return 8 * time.Second
}

// betterTranscript returns the Whisper transcript when configured and usable, else Vosk's.
func betterTranscript(device string, pcm []byte, voskText string) string {
	httpBase, wyAddr := os.Getenv("WHISPER_HTTP"), os.Getenv("WYOMING_STT")
	if (httpBase == "" && wyAddr == "") || len(pcm) == 0 {
		return voskText
	}
	timeout := timeoutFromEnv("WHISPER_TIMEOUT", "WYOMING_STT_TIMEOUT")
	t0 := time.Now()
	var text string
	var err error
	if httpBase != "" {
		text, err = whisperHTTPTranscribe(httpBase, pcm, timeout)
	} else {
		text, err = wyomingTranscribe(wyAddr, pcm, timeout)
	}
	took := time.Since(t0).Seconds()
	text = strings.TrimSpace(whisperTag.ReplaceAllString(text, ""))
	voskEmpty := strings.TrimSpace(voskText) == ""
	switch {
	case err != nil:
		fmt.Printf("(Bot %s) Whisper failed after %.1fs (%v); using Vosk\n", device, took, err)
		return voskText
	case voskEmpty && (whisperGhost.MatchString(text) || len(strings.Fields(text)) < 5):
		if text != "" {
			fmt.Printf("(Bot %s) Whisper heard %q where Vosk heard nothing; treating as silence\n", device, text)
		}
		return ""
	case text == "":
		return voskText
	}
	fmt.Printf("(Bot %s) Whisper (%.1fs): %q | Vosk: %q\n", device, took, text, voskText)
	return text
}
