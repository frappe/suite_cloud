package stalwart

import (
	"bytes"
	"errors"
	"fmt"
)

// scanner is a recursive-descent reader over the `key = value, ...` part of a line.
type scanner struct {
	buf []byte
	pos int
}

// pairs reads pairs into the map until the end, or until the "}" that closes a nested event.
func (s *scanner) pairs(into map[string]any, depth int) error {
	for {
		s.skipSpaces()
		if s.eof() || (depth > 0 && s.peek() == '}') {
			return nil
		}
		key := s.key()
		if key == "" {
			return fmt.Errorf("expected a key, found %q", s.ahead(20))
		}
		if !s.consume(" = ") {
			return fmt.Errorf("expected ' = ' after %q", key)
		}
		value, err := s.value(depth, false)
		if err != nil {
			return err
		}
		into[key] = value

		s.skipSpaces()
		if s.eof() || (depth > 0 && s.peek() == '}') {
			return nil
		}
		if !s.consume(",") {
			return fmt.Errorf("expected ',' after %q, found %q", key, s.ahead(20))
		}
	}
}

// value reads one value: a quoted string, an array, a nested event or a bare scalar.
func (s *scanner) value(depth int, inArray bool) (any, error) {
	if s.eof() {
		return nil, errors.New("missing value")
	}
	switch s.peek() {
	case '"':
		return s.quoted(depth, inArray), nil
	case '[':
		return s.array(depth)
	}
	return s.bare(depth, inArray)
}

// quoted reads a string. Stalwart escapes only \r, \n, \t and the backslash, never a quote, so a
// quote ends the string only where the grammar allows the value to end.
func (s *scanner) quoted(depth int, inArray bool) string {
	s.pos++
	var out []byte
	for s.pos < len(s.buf) {
		c := s.buf[s.pos]
		switch {
		case c == '\\' && s.pos+1 < len(s.buf):
			if unescaped, ok := unescape(s.buf[s.pos+1]); ok {
				out = append(out, unescaped)
				s.pos += 2
				continue
			}
		case c == '"' && s.closesString(s.pos+1, depth, inArray):
			s.pos++
			return string(out)
		}
		out = append(out, c)
		s.pos++
	}
	return string(out) // unterminated: the rest of the line is the value
}

// closesString reports whether a string may end right before position at.
func (s *scanner) closesString(at int, depth int, inArray bool) bool {
	rest := s.buf[at:]
	switch {
	case len(rest) == 0:
		return true
	case inArray && (rest[0] == ']' || bytes.HasPrefix(rest, []byte(", "))):
		return true
	case depth > 0 && bytes.HasPrefix(rest, []byte(" }")):
		return true
	case bytes.HasPrefix(rest, []byte(", ")):
		return keyFollows(rest[2:])
	}
	return false
}

// keyFollows reports whether buf starts with `key = `.
func keyFollows(buf []byte) bool {
	i := 0
	for i < len(buf) && isKeyByte(buf[i]) {
		i++
	}
	return i > 0 && bytes.HasPrefix(buf[i:], []byte(" = "))
}

func (s *scanner) array(depth int) ([]any, error) {
	s.pos++
	items := []any{}
	for {
		s.skipSpaces()
		if s.eof() {
			return nil, errors.New("unterminated array")
		}
		if s.peek() == ']' {
			s.pos++
			return items, nil
		}
		item, err := s.value(depth, true)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
		s.skipSpaces()
		if !s.consume(",") && !(!s.eof() && s.peek() == ']') {
			return nil, fmt.Errorf("expected ',' or ']' in array, found %q", s.ahead(20))
		}
	}
}

// bare reads an unquoted token: a scalar, or a nested event `description (event.name)` that may
// carry its own pairs in braces.
func (s *scanner) bare(depth int, inArray bool) (any, error) {
	start := s.pos
	for s.pos < len(s.buf) && !s.endsBare(depth, inArray) {
		s.pos++
	}
	token := string(bytes.TrimSpace(s.buf[start:s.pos]))
	if token == "" {
		return nil, fmt.Errorf("expected a value, found %q", s.ahead(20))
	}

	description, name, isEvent := isEventName(token)
	if !isEvent {
		return scalar(token), nil
	}
	nested := map[string]any{"event": name, "description": description}
	if s.consume("{") {
		if err := s.pairs(nested, depth+1); err != nil {
			return nil, err
		}
		if !s.consume("}") {
			return nil, fmt.Errorf("unterminated braces after %q", name)
		}
	}
	return nested, nil
}

func (s *scanner) endsBare(depth int, inArray bool) bool {
	switch s.peek() {
	case ',', '{':
		return true
	case '}':
		return depth > 0
	case ']':
		return inArray
	}
	return false
}

func (s *scanner) key() string {
	start := s.pos
	for s.pos < len(s.buf) && isKeyByte(s.buf[s.pos]) {
		s.pos++
	}
	return string(s.buf[start:s.pos])
}

func (s *scanner) consume(text string) bool {
	if bytes.HasPrefix(s.buf[s.pos:], []byte(text)) {
		s.pos += len(text)
		return true
	}
	return false
}

func (s *scanner) skipSpaces() {
	for s.pos < len(s.buf) && s.buf[s.pos] == ' ' {
		s.pos++
	}
}

func (s *scanner) eof() bool  { return s.pos >= len(s.buf) }
func (s *scanner) peek() byte { return s.buf[s.pos] }

func (s *scanner) ahead(n int) string {
	end := min(s.pos+n, len(s.buf))
	return string(s.buf[s.pos:end])
}

func isKeyByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-'
}

func unescape(c byte) (byte, bool) {
	switch c {
	case 'r':
		return '\r', true
	case 'n':
		return '\n', true
	case 't':
		return '\t', true
	case '\\':
		return '\\', true
	}
	return 0, false
}
