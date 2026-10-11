# macos-setup: host-side customization of a stopped Lima macOS guest (see main.go).
# Pure Go, no cgo.

BIN := macos-setup

.PHONY: build vet fmt clean

build: $(BIN)

$(BIN): $(wildcard *.go) go.mod go.sum
	CGO_ENABLED=0 go build -o $@ .

vet:
	go vet ./...

fmt:
	gofmt -l -w .

clean:
	rm -f $(BIN)
