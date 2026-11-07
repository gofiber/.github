<h1 align="center">
  <a href="https://gofiber.io">
    <picture>
      <source height="125" media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/gofiber/docs/master/static/img/logo-dark.svg">
      <img height="125" alt="Fiber" src="https://raw.githubusercontent.com/gofiber/docs/master/static/img/logo.svg">
    </picture>
  </a>
</h1>

<p align="center">
  <b>Fiber</b> is an Express-inspired web framework for Go, built on top of
  <a href="https://github.com/valyala/fasthttp">Fasthttp</a> and designed for
  fast development with zero memory allocation and performance in mind.
</p>

<p align="center">
  <a href="https://docs.gofiber.io">📚 Docs</a> ·
  <a href="https://gofiber.io/discord">💬 Discord</a> ·
  <a href="https://gofiber.github.io/fiber/benchmarks/">📊 Benchmarks</a> ·
  <a href="https://github.com/sponsors/gofiber">⭐ Sponsor</a>
</p>

## 🚀 Get started

```bash
go get github.com/gofiber/fiber/v3
```

```go
package main

import (
    "log"

    "github.com/gofiber/fiber/v3"
)

func main() {
    app := fiber.New()

    app.Get("/", func(c fiber.Ctx) error {
        return c.SendString("Hello, World 👋!")
    })

    log.Fatal(app.Listen(":3000"))
}
```

## 📦 Ecosystem

| Repository | Description |
| --- | --- |
| [fiber](https://github.com/gofiber/fiber) | The web framework, including the core middleware |
| [contrib](https://github.com/gofiber/contrib) | Additional middleware with third-party dependencies |
| [storage](https://github.com/gofiber/storage) | Premade storage drivers for sessions, caching, rate limiting and more |
| [template](https://github.com/gofiber/template) | Template engines for rendering views |
| [schema](https://github.com/gofiber/schema) | Converts structs to and from form values |
| [utils](https://github.com/gofiber/utils) | Fast, low-allocation helper functions |
| [cli](https://github.com/gofiber/cli) | Command line tool for Fiber projects |
| [recipes](https://github.com/gofiber/recipes) | Ready-to-run examples for common use cases |
| [boilerplate](https://github.com/gofiber/boilerplate) | Starter project for new Fiber applications |
| [awesome-fiber](https://github.com/gofiber/awesome-fiber) | Curated list of middleware, tools, articles and projects |
| [benchmarks](https://github.com/gofiber/benchmarks) | Request benchmarks for Fiber v2 and v3 |
| [docs](https://github.com/gofiber/docs) | Source of [docs.gofiber.io](https://docs.gofiber.io) |

## 🤝 Contributing

Contributions of every size are welcome. Please read the
[contributing guide](https://github.com/gofiber/.github/blob/main/CONTRIBUTING.md)
and our
[code of conduct](https://github.com/gofiber/.github/blob/main/CODE_OF_CONDUCT.md)
first. Questions are best asked on [Discord](https://gofiber.io/discord);
security issues are reported as described in the
[security policy](https://github.com/gofiber/.github/blob/main/SECURITY.md).

## ❤️ Support Fiber

Fiber is free and open source. If it helps you or your company, consider
[sponsoring the project](https://github.com/sponsors/gofiber).
