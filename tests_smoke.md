# InboxPilot - Smoke Tests

Comandos curl para testar os endpoints do sistema.

## 1. Listar Emails (Unified Inbox)

```bash
# Listar emails de hoje
curl -s "http://localhost:5000/inbox/list?range=today&providers=apple,gmail&folders=inbox" | python -m json.tool

# Listar emails da semana
curl -s "http://localhost:5000/inbox/list?range=week&providers=apple,gmail&folders=inbox,spam&limit_per_provider=10"
```

## 2. Ver Detalhes de um Email

```bash
# Substituir {key} pelo key do email (ex: apple:49905)
curl -s "http://localhost:5000/inbox/message/apple:49905" | python -m json.tool
```

## 3. Sugerir Resposta

```bash
curl -s -X POST "http://localhost:5000/inbox/message/apple:49905/suggest-reply" | python -m json.tool
```

## 4. Adicionar à Fila

```bash
# Marcar como lido
curl -s -X POST "http://localhost:5000/queue/add" \
  -H "Content-Type: application/json" \
  -d '{"key":"apple:49905","action":"mark_read"}' | python -m json.tool

# Adicionar para envio com resposta
curl -s -X POST "http://localhost:5000/queue/add" \
  -H "Content-Type: application/json" \
  -d '{"key":"apple:49905","action":"send","reply":{"body":"Obrigado pelo contato!"}}' | python -m json.tool

# Marcar para deletar
curl -s -X POST "http://localhost:5000/queue/add" \
  -H "Content-Type: application/json" \
  -d '{"key":"apple:49905","action":"delete"}' | python -m json.tool
```

## 5. Ver Fila

```bash
curl -s "http://localhost:5000/queue" | python -m json.tool
```

## 6. Exportar para ChatGPT

```bash
curl -s "http://localhost:5000/export/chatgpt?range=today&providers=apple,gmail&folders=inbox"
```

## 7. Exportar Despacho JSON

```bash
curl -s "http://localhost:5000/export/dispatch.json" | python -m json.tool
```

## 8. Executar Fila (Commit)

```bash
# Executar todos os itens pending
curl -s -X POST "http://localhost:5000/queue/commit" | python -m json.tool

# Executar com JSON específico
curl -s -X POST "http://localhost:5000/queue/commit" \
  -H "Content-Type: application/json" \
  -d '{
    "dispatch_type": "inboxpilot_batch",
    "actions": [
      {"key": "apple:49905", "provider": "apple", "action": "mark_read"}
    ]
  }' | python -m json.tool
```

## 9. Dashboard Web

Acessar via navegador:
```
http://localhost:5000/ui
```

## 10. Com API Key (se configurada)

```bash
# Adicionar header X-API-Key
curl -s -H "X-API-Key: sua-chave" "http://localhost:5000/inbox/list?range=today&providers=apple"
```

## Validação Rápida

```bash
echo "=== Teste 1: /inbox/list ===" && \
curl -s -o /dev/null -w "%{http_code}" "http://localhost:5000/inbox/list?range=today&providers=apple&folders=inbox" && \
echo "" && \
echo "=== Teste 2: /queue ===" && \
curl -s -o /dev/null -w "%{http_code}" "http://localhost:5000/queue" && \
echo "" && \
echo "=== Teste 3: /export/dispatch.json ===" && \
curl -s -o /dev/null -w "%{http_code}" "http://localhost:5000/export/dispatch.json" && \
echo "" && \
echo "=== Teste 4: /ui ===" && \
curl -s -o /dev/null -w "%{http_code}" "http://localhost:5000/ui"
```
