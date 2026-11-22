package wirepod_vosk

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/kercre123/wire-pod/chipper/pkg/logger"
)

// captureSample saves one request's audio (16 kHz mono s16le PCM, as Vosk heard it) and
// Vosk's transcript, to compare speech recognisers on real requests. Opt-in and local:
// only when STT_CAPTURE_DIR is set; keeps the newest STT_CAPTURE_MAX clips (default 60).
func captureSample(pcm []byte, text string) {
	dir := os.Getenv("STT_CAPTURE_DIR")
	if dir == "" || len(pcm) == 0 {
		return
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		logger.Println("STT capture: " + err.Error())
		return
	}
	base := filepath.Join(dir, time.Now().Format("20060102-150405.000"))
	_ = os.WriteFile(base+".pcm", pcm, 0600)
	_ = os.WriteFile(base+".txt", []byte(text), 0600)
	max := 60
	if v, err := strconv.Atoi(os.Getenv("STT_CAPTURE_MAX")); err == nil && v > 0 {
		max = v
	}
	files, _ := filepath.Glob(filepath.Join(dir, "*.pcm"))
	sort.Strings(files)
	for len(files) > max {
		old := strings.TrimSuffix(files[0], ".pcm")
		_ = os.Remove(old + ".pcm")
		_ = os.Remove(old + ".txt")
		files = files[1:]
	}
}
