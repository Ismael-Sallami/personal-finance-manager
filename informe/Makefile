# Build the report:  make        -> informe.pdf
# Clean artefacts:   make clean
MAIN = informe

all: $(MAIN).pdf

$(MAIN).pdf: $(MAIN).tex chapters/*.tex
	latexmk -pdf -interaction=nonstopmode -halt-on-error $(MAIN).tex

clean:
	latexmk -C
	rm -f *.aux *.log *.out *.toc *.fls *.fdb_latexmk chapters/*.aux

.PHONY: all clean
