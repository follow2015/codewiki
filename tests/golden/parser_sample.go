// Package sample 是黄金样本：Go 后端改解析行为会让 digest 变化。
package sample

import (
	"fmt"
	"os"
)

const MAX = 3

type Outer struct {
	Inner int
}

type Sub struct {
	Outer
}

// Run 的方法名与 Sub.Run 相同，靠接收者区分 qualname。
func (o Outer) Run(x int) int {
	return Helper(x)
}

func (s Sub) Run(x int) int {
	return s.Outer.Run(x) + MAX
}

func Helper(v int) int {
	fmt.Println(os.Args)
	return v + MAX
}
