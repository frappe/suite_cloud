package prometheus

import (
	"bufio"
	"bytes"
	"fmt"
	"strconv"
	"strings"
)

// Family is one metric of the exposition with every sample that belongs to it; a histogram's
// _bucket, _sum and _count samples are its samples.
type Family struct {
	Name    string
	Type    string
	Samples []Sample
}

type Sample struct {
	Name   string
	Labels map[string]string
	Value  float64
}

// Parse reads the text exposition format. A sample without a TYPE line counts as a gauge.
func Parse(text []byte) ([]Family, error) {
	var families []Family
	index := map[string]int{}
	alias := map[string]string{}
	family := func(name string) *Family {
		if base, ok := alias[name]; ok {
			name = base
		}
		if i, ok := index[name]; ok {
			return &families[i]
		}
		families = append(families, Family{Name: name, Type: "gauge"})
		index[name] = len(families) - 1
		return &families[len(families)-1]
	}

	scanner := bufio.NewScanner(bytes.NewReader(text))
	scanner.Buffer(make([]byte, 0, 64<<10), 8<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		switch {
		case line == "", strings.HasPrefix(line, "# HELP"):
		case strings.HasPrefix(line, "# TYPE "):
			fields := strings.Fields(line)
			if len(fields) != 4 {
				return nil, fmt.Errorf("bad TYPE line %q", line)
			}
			f := family(fields[2])
			f.Type = fields[3]
			if f.Type == "histogram" || f.Type == "summary" {
				for _, suffix := range []string{"_bucket", "_sum", "_count"} {
					alias[fields[2]+suffix] = fields[2]
				}
			}
		case strings.HasPrefix(line, "#"):
		default:
			sample, err := parseSample(line)
			if err != nil {
				return nil, err
			}
			f := family(sample.Name)
			f.Samples = append(f.Samples, sample)
		}
	}
	return families, scanner.Err()
}

// parseSample reads `name{label="value",...} value [timestamp]`.
func parseSample(line string) (Sample, error) {
	sample := Sample{}
	rest := line
	if open := strings.IndexByte(line, '{'); open >= 0 {
		closing := strings.LastIndexByte(line, '}')
		if closing < open {
			return sample, fmt.Errorf("bad labels in %q", line)
		}
		sample.Name = line[:open]
		labels, err := parseLabels(line[open+1 : closing])
		if err != nil {
			return sample, fmt.Errorf("%q: %w", line, err)
		}
		sample.Labels = labels
		rest = line[closing+1:]
	} else {
		name, after, found := strings.Cut(line, " ")
		if !found {
			return sample, fmt.Errorf("no value in %q", line)
		}
		sample.Name, rest = name, after
	}
	fields := strings.Fields(rest)
	if len(fields) == 0 {
		return sample, fmt.Errorf("no value in %q", line)
	}
	value, err := strconv.ParseFloat(fields[0], 64)
	if err != nil {
		return sample, fmt.Errorf("%q: %w", line, err)
	}
	sample.Value = value
	return sample, nil
}

func parseLabels(text string) (map[string]string, error) {
	labels := map[string]string{}
	for len(text) > 0 {
		name, rest, found := strings.Cut(text, "=\"")
		if !found {
			return nil, fmt.Errorf("bad label %q", text)
		}
		value, after, ok := cutQuoted(rest)
		if !ok {
			return nil, fmt.Errorf("unterminated label %q", text)
		}
		labels[strings.TrimSpace(name)] = value
		text = strings.TrimPrefix(strings.TrimSpace(after), ",")
	}
	return labels, nil
}

// cutQuoted reads up to the closing quote, honouring \" \\ and \n.
func cutQuoted(text string) (value, rest string, ok bool) {
	var out strings.Builder
	for i := 0; i < len(text); i++ {
		switch c := text[i]; c {
		case '\\':
			if i+1 < len(text) {
				i++
				if text[i] == 'n' {
					out.WriteByte('\n')
				} else {
					out.WriteByte(text[i])
				}
			}
		case '"':
			return out.String(), text[i+1:], true
		default:
			out.WriteByte(c)
		}
	}
	return "", "", false
}
