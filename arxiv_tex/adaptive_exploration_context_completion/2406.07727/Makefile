# Makefile for LaTeX document compilation

# Change these variables according to your needs
TEX_SOURCE = MHR.tex
PDF_OUTPUT = $(TEX_SOURCE:.tex=.pdf)

# LaTeX compiler
LATEX = pdflatex

# LaTeX flags
LATEX_FLAGS = -interaction=nonstopmode

# Default target
all: $(PDF_OUTPUT)

# Target to compile LaTeX source file to PDF
$(PDF_OUTPUT): $(TEX_SOURCE)
	-$(LATEX) $(LATEX_FLAGS) $<
# Clean up auxiliary files
clean:
	rm -f *.aux *.log *.out *.toc 

# Remove PDF output
cleanpdf:
	rm -f $(PDF_OUTPUT)
