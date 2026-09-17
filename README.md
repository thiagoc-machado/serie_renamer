# Series Renamer

Aplicação leve em FastAPI para:

- varrer uma pasta de mídia atrás de arquivos de série
- agrupar por pasta e por padrão de nome, sem depender de adivinhação online
- renomear no formato compatível com Sonarr/Jellyfin
- mover arquivos com segurança, sem sobrescrever e sem apagar mídia
- escrever metadados MP4 básicos
- gerar preview antes de aplicar
- salvar histórico com desfazer
- listar pastas vazias para validação e remoção segura
- listar arquivos com metadados divergentes
- editar e remover aliases aprendidos
- separar a navegação por abas de biblioteca e por abas funcionais

## Bibliotecas já configuradas

- `Series` → `/media/series`
- `Filmes` → `/media/movies`
- `Cristaos` → `/media/cristaos`
- `Livros` → `/media/livros`
- `Audiolivros` → `/media/Audio-livros`

## Formato gerado

Os arquivos são movidos para:

`/media/Nome da Série/Season 01/Nome da Série - S01E02 - Título Opcional.mp4`

Se o título do episódio ficar vazio:

`/media/Nome da Série/Season 01/Nome da Série - S01E02.mp4`

## Subir com Docker

Os mounts já estão preparados no `docker-compose.yml`:

```yaml
volumes:
  - /opt/appdata/jellyfin:/config
  - /mnt/storage/movies:/media/movies
  - /mnt/storage/series:/media/series
  - /mnt/storage/cristaos:/media/cristaos
  - /mnt/storage/Livros:/media/livros
  - /mnt/storage/Audio-livros:/media/Audio-livros
```

Depois:

```bash
docker compose up -d --build
```

Ou com `make`:

```bash
make up
```

Abra:

`http://SEU_SERVIDOR:8085`

## Observações

- O app só processa `mp4`, `m4v` e `mov`
- Os padrões aceitos incluem `CODIGOT01EP02`, `CODIGO_S01E02` e `CODIGO 1x02`
- O app não tenta adivinhar série pela internet por padrão
- A aba de séries usa a pasta local como fonte principal do nome
- A aba de padrões mostra somente arquivos ainda fora do padrão Sonarr/Jellyfin
- A aba de metadados mostra divergências entre pasta e tag `tvsh`
- A aba de vazios lista apenas pastas sem conteúdo
- Os metadados MP4 gravados incluem título, nome da série, temporada e episódio
- O histórico e as siglas aprendidas ficam em `/config/series-renamer`
- A interface abre com abas separadas para cada biblioteca montada
- A busca online de títulos ficou opcional e fora do fluxo principal

## Integrações opcionais

Se quiser disparar atualização automática após um lote, preencha no `docker-compose.yml`:

```yaml
environment:
  SONARR_URL: "http://sonarr:8989"
  SONARR_API_KEY: "sua-chave"
  JELLYFIN_URL: "http://jellyfin:8096"
  JELLYFIN_API_KEY: "sua-chave"
  JELLYFIN_LIBRARY_ID: "id-da-biblioteca"
  TMDB_BEARER_TOKEN: "seu-bearer-token"
  TMDB_DEFAULT_LANGUAGE: "pt-BR"
```

## Fluxo recomendado

1. Varra a pasta.
2. Confirme ou ajuste o nome das séries.
3. Use `Gerar preview`.
4. Opcionalmente corrija metadados ou remova pastas vazias.
5. Aplique a renomeação.
6. Se algo sair errado, use o `batch_id` para desfazer.

## Comandos úteis

```bash
make up
make down
make test
```

Se o host não tiver `make`, use:

```bash
./up.sh
./down.sh
./test.sh
```
