add_library(Freetype::Freetype INTERFACE IMPORTED)
set_target_properties(Freetype::Freetype PROPERTIES
        INTERFACE_COMPILE_OPTIONS "-sUSE_FREETYPE=1"
        INTERFACE_LINK_LIBRARIES "-sUSE_FREETYPE=1"
)

set(FREETYPE_FOUND on)
set(Freetype_FOUND on)
