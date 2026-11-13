/* Declarations-only stand-in for <openobex/obex.h>, used by
   scripts/check-dumpscribe.sh to syntax-check vendor/dumpscribe.c on
   machines without libopenobex. It is never linked or shipped. */
#include <stdint.h>
typedef struct obex obex_t;
typedef struct obex_object obex_object_t;
typedef union { uint32_t bq4; uint8_t bq1; const uint8_t *bs; } obex_headerdata_t;
typedef struct { struct { char *manufacturer, *product, *serial, *configuration, *control_interface, *data_interface_idle, *data_interface_active; int idVendor, idProduct, bus_number, device_address, interface_number; } usb; } obex_interface_t;
typedef void (*obex_event_t)(obex_t *, obex_object_t *, int, int, int, int);
enum { OBEX_CMD_CONNECT, OBEX_CMD_GET, OBEX_EV_ABORT, OBEX_EV_CONTINUE, OBEX_EV_LINKERR, OBEX_EV_PROGRESS, OBEX_EV_REQDONE, OBEX_EV_STREAMAVAIL, OBEX_FINAL, OBEX_FL_FIT_ONE_PACKET, OBEX_HDR_BODY, OBEX_HDR_BODY_END, OBEX_HDR_CONNECTION, OBEX_HDR_NAME, OBEX_HDR_TARGET, OBEX_MAXIMUM_MTU, OBEX_RSP_CONTINUE, OBEX_RSP_NOT_FOUND, OBEX_RSP_SUCCESS, OBEX_TRANS_USB };
int OBEX_CancelRequest(); void OBEX_Cleanup(); int OBEX_EnumerateInterfaces(); obex_interface_t *OBEX_GetInterfaceByIndex(); void *OBEX_GetUserData(); int OBEX_HandleInput(); obex_t *OBEX_Init(); int OBEX_InterfaceConnect(); int OBEX_ObjectAddHeader(); int OBEX_ObjectDelete(); int OBEX_ObjectGetNextHeader(); obex_object_t *OBEX_ObjectNew(); int OBEX_ObjectReadStream(); int OBEX_Request(); int OBEX_SetTransportMTU(); void OBEX_SetUserData();
