package telemetry

import (
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"sync"
	"time"
)

var (
	uuidMu       sync.Mutex
	uuidLastMs   int64
	uuidSequence uint16
)

// NewUUIDv7 returns a time-ordered id. Suite Cloud loads staged batches in id order, so ids made
// within one millisecond count up instead of being random.
func NewUUIDv7() string {
	uuidMu.Lock()
	ms := time.Now().UnixMilli()
	if ms <= uuidLastMs {
		ms = uuidLastMs
		uuidSequence++
		if uuidSequence > 0x0fff {
			ms++
			uuidSequence = 0
		}
	} else {
		uuidSequence = 0
	}
	uuidLastMs = ms
	sequence := uuidSequence
	uuidMu.Unlock()

	var id [16]byte
	binary.BigEndian.PutUint64(id[:8], uint64(ms)<<16)
	id[6] = 0x70 | byte(sequence>>8)
	id[7] = byte(sequence)
	if _, err := rand.Read(id[8:]); err != nil {
		panic("crypto/rand: " + err.Error())
	}
	id[8] = 0x80 | (id[8] & 0x3f)

	var text [36]byte
	hex.Encode(text[:8], id[:4])
	text[8] = '-'
	hex.Encode(text[9:13], id[4:6])
	text[13] = '-'
	hex.Encode(text[14:18], id[6:8])
	text[18] = '-'
	hex.Encode(text[19:23], id[8:10])
	text[23] = '-'
	hex.Encode(text[24:], id[10:])
	return string(text[:])
}
