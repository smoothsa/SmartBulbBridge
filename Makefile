SRCDIR ?= /opt/fpp/src
include ${SRCDIR}/makefiles/common/setup.mk
include $(SRCDIR)/makefiles/platform/*.mk

all: libfpp-co-SmartBulbBridge.$(SHLIB_EXT)
debug: all

OBJECTS_sbb += src/SmartBulbOutput.o
LIBS_sbb += -L${SRCDIR} -lfpp
CXXFLAGS_src/SmartBulbOutput.o += -I${SRCDIR} -I${SRCDIR}/channeloutput

%.o: %.cpp Makefile
	$(CCACHE) $(CC) $(CFLAGS) $(CXXFLAGS) $(CXXFLAGS_$@) -c $< -o $@

libfpp-co-SmartBulbBridge.$(SHLIB_EXT): $(OBJECTS_sbb) ${SRCDIR}/libfpp.$(SHLIB_EXT)
	$(CCACHE) $(CC) -shared $(CFLAGS_$@) $(OBJECTS_sbb) $(LIBS_sbb) $(LDFLAGS) -o $@

clean:
	rm -f libfpp-co-SmartBulbBridge.so $(OBJECTS_sbb)
