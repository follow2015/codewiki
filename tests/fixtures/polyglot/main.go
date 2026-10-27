package main

import "fmt"

// Handler 处理入口。
type Handler struct {
	name string
}

func (h *Handler) Serve() error {
	fmt.Println(h.name)
	return nil
}
