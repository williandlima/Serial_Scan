# Serial Scan

Analisador de barramentos seriais **RS-232 / RS-485 / RS-422** que descobre
sozinho a configuração da linha, mostra os frames, separa cada comando novo
que aparece e guarda um rótulo para cada um.

Os cinco passos do projeto, na ordem:

| # | Passo | Onde está |
|---|-------|-----------|
| 1 | Selecionar o protocolo (232/485/422) | `serial_scan/protocols.py` |
| 2 | Identificar a configuração do frame automaticamente | `serial_scan/autodetect.py` |
| 3 | Mostrar os dados do frame | `serial_scan/framing.py` |
| 4 | Identificar e segregar cada comando novo | `serial_scan/commands.py` |
| 5 | Colocar um rótulo em cada comando | `serial_scan/labels.py` |

---

## Instalação

```bash
git clone https://github.com/williandlima/Serial_Scan
cd Serial_Scan
pip install -e .
```

Requer Python 3.10 ou mais novo. A única dependência é o `pyserial`. Para a
interface gráfica é preciso ter o Tk instalado (no Ubuntu/Debian:
`sudo apt install python3-tk`; no Windows e no macOS já vem junto com o
Python).

## Comece por aqui (sem hardware nenhum)

O app traz um barramento simulado que gera tráfego **bit a bit**, como um
transmissor de verdade faria no fio. Dá para ver o fluxo inteiro funcionando
antes de encostar em qualquer equipamento:

```bash
serial-scan demo --frames
```

```
Configuracao identificada: 19200 8N1 (82%)
  velocidade medida: 19,200 bps (16.00 amostras/bit)
  silencio entre frames: 1.823 ms

    tempo dir   tam crc      bytes                              comando
   0.0063 REQ    8B crc:ok   01 03 00 6B 00 03 74 17            L8:01-03
   0.0130 RSP   11B crc:ok   01 03 06 00 2A 00 64 01 00 78 FC   L11:01-03
   0.1198 REQ    8B crc:ok   01 06 00 10 00 01 49 CF            L8:01-06
  >> comando novo identificado: L8:01-06

212 frames / 1996 bytes em 1.99 s de barramento | checksum: CRC-16/MODBUS
  checksum valido em 211/211 frames

assinatura   rotulo       n     % dir    crc  exemplo
L8:01-03     -           34   16% REQ   100%  01 03 00 6B 00 03 74 17
L11:01-03    -           34   16% RSP   100%  01 03 06 00 2A 00 64 01 00 78 FC
...
```

## Uso com hardware

```bash
serial-scan portas                                   # que portas existem
serial-scan protocolos                               # o que muda entre 232/485/422

# passo 2 sozinho: só descobrir a configuração
serial-scan detectar --porta /dev/ttyUSB0 --protocolo 485

# fluxo completo: detecta, captura, segrega e salva os rótulos
serial-scan capturar --porta /dev/ttyUSB0 --protocolo 485 --auto \
    --duracao 60 --gravar captura.jsonl --campos

# reanalisar depois, sem o equipamento por perto
serial-scan reproduzir captura.jsonl --campos

# rótulos (passo 5)
serial-scan rotulos --definir "L8:01-03=Leitura de temperatura"
serial-scan rotulos
```

### As três formas de rodar

```bash
serial-scan demo                      # comando instalado
python -m serial_scan demo            # como módulo
python serial_scan/__main__.py demo   # arquivo solto (botão Run do VS Code)
```

As três são equivalentes. A terceira funciona mesmo sem `pip install`, porque
o `__main__.py` cai num import absoluto quando percebe que foi executado fora
do contexto de pacote.

### Interface gráfica

```bash
serial-scan gui
```

Uma janela com os cinco passos na ordem: escolher protocolo e porta, botão
**Identificar automaticamente**, tabela de frames ao vivo, lista de comandos
segregados à esquerda (os novos aparecem destacados) e um campo de rótulo
que salva em disco na hora. Clicar em um comando mostra o mapa de campos e
permite filtrar a tabela de frames só por ele.

---

## Como funciona a identificação automática (passo 2)

Esta é a parte que exige mais cuidado, então vale explicar o que o app pode e
o que ele **não** pode saber.

### Estratégia 1 — varredura de candidatos (`scan`)

Funciona com qualquer conversor USB-serial comum. O app abre a porta em uma
configuração, escuta por um instante, e dá uma nota ao que saiu. Repete.

Uma varredura ingênua testaria 19 velocidades × 10 formatos = 190 tentativas,
o que a 0,8 s cada dá dois minutos e meio. O app faz em duas fases:

1. **acha a velocidade**, testando cada baud rate com 8N1, 8E1 e 7E1;
2. **varre os formatos** só nas velocidades mais promissoras.

Na prática são 10 a 40 tentativas, e ela para assim que encontra algo
convincente.

**Como uma configuração ganha nota.** O ponto delicado: um barramento com um
laço de polling decodificado na velocidade *errada* produz lixo que se repete
com a mesma fidelidade que o tráfego certo. Repetição e consistência de
tamanho, sozinhas, dão nota máxima para lixo. Por isso a nota não é uma soma
ponderada: a *estrutura* (repetição, tamanhos, ausência de 0x00/0xFF em
excesso) é multiplicada por uma **evidência** de que os bytes significam
alguma coisa — um checksum que fecha, ou um fluxo que é texto legível de
ponta a ponta. Sem evidência, a confiança fica travada em cerca de 35%.

### Estratégia 2 — sobreamostragem (`oversample`)

Se as amostras do nível do fio estiverem disponíveis (do simulador, de um
analisador lógico, ou de um adaptador aberto a uma taxa muito maior que a da
linha), o app decodifica a UART em software. Aí ele:

* **mede** o tempo de bit em vez de adivinhar (o menor pulso do fluxo é
  exatamente um bit; os outros são múltiplos inteiros dele);
* testa todos os formatos sobre **os mesmos dados**;
* **enxerga o bit de paridade**, o que a estratégia 1 não consegue.

### O que é impossível saber pelos bytes

Algumas configurações produzem **exatamente os mesmos bytes de dados**:

* **8E1 e 8O1** — só o bit de paridade difere, e ele não aparece nos dados;
* **7E1 e 7O1** — idem;
* **8N1 e 8N2** — o segundo stop bit é apenas mais tempo de linha em repouso.

O app não finge escolher. Ele agrupa essas configurações em uma classe de
equivalência, elege a mais permissiva como representante (menos stop bits,
sem paridade) e diz na saída quais são as outras:

```
Configuracao identificada: 9600 8N1 (82%) [bytes identicos em: 8E1, 8O1, 8N2]
```

Para *ler* o barramento, qualquer uma serve. A estratégia de sobreamostragem
desempata quando os bits estão visíveis.

Existe ainda um caso que **é** resolvível e que o app trata: uma linha 7E1
lida como 8N1 não gera nenhum erro de enquadramento — o receptor apenas
guarda o bit de paridade como bit mais significativo do dado. Se o bit 7 de
quase todo byte for a paridade par (ou ímpar) dos sete bits de baixo, a linha
é 7E1 (ou 7O1) e não 8N1. O app testa isso e reporta.

---

## Como os comandos são segregados (passo 4)

Um "comando" é uma classe de frames que compartilha os bytes que o
identificam. A assinatura padrão é `L<tamanho>:<bytes-chave>` — por exemplo
`L8:01-03` é um frame de 8 bytes cujos bytes 0 e 1 são `01 03`.

O tamanho entra na identidade de propósito: na maioria dos protocolos a
pergunta e a resposta compartilham endereço e função e diferem no tamanho, e
separá-las é justamente a segregação que interessa.

**Quais bytes identificam o comando é descoberto também.** Um identificador
assume poucos valores distintos ao longo da captura: um só valor é um
cabeçalho constante (não identifica nada) e dezenas de valores são payload.
O teto do que conta como "poucos" é relativo ao volume capturado — doze
valores em doze frames é um número de sequência, não um código de função.
`--autotune` na CLI, ou o botão **Reagrupar** na interface, adota a sugestão
e reconstrói o catálogo a partir do que já foi capturado, **sem perder os
rótulos**.

**Mapa de campos.** Com várias amostras do mesmo comando, comparar byte a
byte mostra o que é constante, o que alterna entre poucos valores, o que
conta de um em um e o que é payload livre:

```
Mapa de campos de L11:01-03 (34 amostras):
  byte  0: chave       0x01 (chave)
  byte  1: chave       0x03 (chave)
  byte  2: constante   0x06
  byte  4: contador    contador (+1)
  byte  9: checksum    checksum
```

---

## Detalhes que valem saber

**Enquadramento por silêncio.** Protocolos binários raramente trazem um campo
de tamanho em que dá para confiar antes de conhecer o protocolo. O que eles
têm é tempo: um frame é uma rajada de caracteres seguida de silêncio. A regra
do Modbus RTU (3,5 tempos de caractere) é o padrão, mas o app mede a
distribuição real dos intervalos entre bytes e escolhe o corte onde ela se
separa em duas populações.

**Checksum.** O app testa CRC-16 (Modbus, CCITT, Kermit, XMODEM), CRC-8,
soma, XOR e LRC no fim dos frames. Se um deles fecha em dezenas de frames de
tamanhos diferentes, a configuração está certa — é a confirmação mais forte
que existe. Os frames capturados antes da identificação são reverificados.

**Sentido do tráfego (REQ/RSP).** No RS-485 os dois sentidos dividem o mesmo
par, então a captura é um fluxo intercalado. O app separa por tempo: frame
depois de um silêncio longo inicia uma transação, frame logo em seguida é a
resposta. É heurística, e está marcada como tal — quebra com relatórios
espontâneos de escravo e com mestres que disparam rajadas de perguntas.

**Rótulos.** Ficam em `serial_scan_labels.json`, uma seção por protocolo,
gravados de forma atômica. Sobrevivem a reagrupamento, reconfiguração e novas
sessões. É a única parte da análise que não dá para recalcular: todo o resto
sai da captura, mas "isto abre a válvula 3" só existe porque alguém digitou.

---

## Arquitetura

```
serial_scan/
  protocols.py   perfis RS-232/485/422: fios, duplex, offsets de chave, gaps
  portconfig.py  SerialConfig (baud, bits, paridade, stop) e temporização
  uart.py        UART em software: codifica/decodifica amostras, mede bit time
  framing.py     corta o fluxo em frames pelo silêncio entre caracteres
  checksums.py   CRCs e detecção de qual algoritmo o barramento usa
  scoring.py     dá nota a uma configuração candidata
  autodetect.py  as duas estratégias de identificação e o relatório
  commands.py    catálogo de comandos, assinaturas, mapa de campos
  labels.py      persistência dos rótulos
  sources.py     porta real, replay de arquivo, barramento simulado
  session.py     amarra tudo: captura, thread, eventos, relatório
  cli.py         linha de comando
  ui/app.py      interface gráfica (Tkinter)
```

O núcleo não importa `pyserial` nem `tkinter` no topo: os dois são carregados
sob demanda, então a biblioteca funciona e é testável em máquinas sem porta
serial e sem Tk.

## Testes

```bash
pip install -e ".[dev]"
python -m pytest
```

O `[dev]` é o que traz o pytest: `pip install -e .` sozinho instala só o
`pyserial`. E `python -m pytest` em vez de `pytest` direto dispensa que o
diretório de scripts do Python esteja no PATH — detalhe que morde no Windows.

290 testes. A auto-detecção é verificada ponta a ponta contra o simulador,
que renderiza o tráfego como níveis lógicos no fio e depois o decodifica com
a configuração que estiver sendo testada — um palpite errado produz bytes
genuinamente corrompidos, não uma imitação de corrupção.

## Licença

MIT.
