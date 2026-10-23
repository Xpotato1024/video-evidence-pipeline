// Comparison fixture only: unsigned, local input; not a production installer.
package main

import (
 "bytes"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "errors"
 "fmt"
 "io"
 "os"
 "os/exec"
 "path/filepath"
 "strings"
)

type Entry struct { Path string `json:"path"`; Size uint64 `json:"size"`; SHA256 string `json:"sha256"` }
type Manifest struct { Schema string `json:"schema"`; Files []Entry `json:"files"` }
func fail(code string) error { return errors.New(code) }
func safeRelative(value string) bool {
 if len(value)==0 || len(value)>240 { return false }
 for _,part:=range strings.Split(value,"/") {
  if part=="" || part=="." || part==".." || strings.HasSuffix(part,".") { return false }
  for _,b:=range []byte(part) {if !(b>='a'&&b<='z'||b>='A'&&b<='Z'||b>='0'&&b<='9'||strings.ContainsRune("._-",rune(b))) {return false}}
  base:=strings.ToUpper(strings.Split(part,".")[0]); for _,reserved:=range []string{"CON","PRN","AUX","NUL","COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9","LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9"} {if base==reserved {return false}}
 }; return true
}
func plain(path string) error { m,e:=os.Lstat(path); if e!=nil{return fail("PATH_UNAVAILABLE")}; if m.Mode()&os.ModeSymlink!=0 || m.Mode()&os.ModeIrregular!=0 {return fail("LINK_FORBIDDEN")};return nil }
func run(args []string)(any,error) {
 if len(args)==1 && args[0]=="capabilities" {return map[string]any{"schema":"vep.poc.v1","operations":[]string{"capabilities","verify","extract","launch"}},nil}
 if len(args)==3 && args[0]=="launch" {c:=exec.Command(args[1],"-I","-S",args[2]); c.Stdin=os.Stdin;c.Stdout=os.Stdout;c.Stderr=os.Stderr;if c.Run()!=nil{return nil,fail("WORKER_FAILED")};return map[string]any{"ok":true},nil}
 if !(len(args)==3&&args[0]=="verify"||len(args)==4&&args[0]=="extract") {return nil,fail("INVALID_REQUEST")}
 m,e:=os.Stat(args[1]);if e!=nil||m.Size()>65536{return nil,fail("MANIFEST_INVALID")};data,e:=os.ReadFile(args[1]);if e!=nil{return nil,fail("MANIFEST_INVALID")}
 var manifest Manifest; d:=json.NewDecoder(bytes.NewReader(data));d.DisallowUnknownFields();if d.Decode(&manifest)!=nil||d.Decode(new(any))!=io.EOF||manifest.Schema!="vep.poc.v1"||len(manifest.Files)==0||len(manifest.Files)>4096{return nil,fail("MANIFEST_INVALID")}
 if e=plain(args[2]);e!=nil{return nil,e};seen:=map[string]bool{};var total uint64
 for _,entry:=range manifest.Files {key:=strings.ToLower(entry.Path); hash,err:=hex.DecodeString(entry.SHA256);if !safeRelative(entry.Path)||seen[key]||entry.Size>64*1024*1024||err!=nil||len(hash)!=32||strings.ToLower(entry.SHA256)!=entry.SHA256{return nil,fail("MANIFEST_INVALID")};seen[key]=true;total+=entry.Size;if total>256*1024*1024{return nil,fail("MANIFEST_INVALID")}}
 if args[0]=="extract" {if os.Mkdir(args[3],0700)!=nil{return nil,fail("DESTINATION_EXISTS")}}
 for _,entry:=range manifest.Files {path:=args[2];for _,part:=range strings.Split(entry.Path,"/"){path=filepath.Join(path,part);if e=plain(path);e!=nil{return nil,e}}
  input,err:=os.Open(path);if err!=nil{return nil,fail("PAYLOAD_INVALID")};meta,err:=input.Stat();if err!=nil||!meta.Mode().IsRegular()||uint64(meta.Size())!=entry.Size{input.Close();return nil,fail("PAYLOAD_INVALID")}
  hash:=sha256.New();var writer io.Writer=hash;var output *os.File
  if args[0]=="extract" {dest:=filepath.Join(args[3],entry.Path);if os.MkdirAll(filepath.Dir(dest),0700)!=nil{input.Close();return nil,fail("COPY_FAILED")};output,err=os.OpenFile(dest,os.O_WRONLY|os.O_CREATE|os.O_EXCL,0600);if err!=nil{input.Close();return nil,fail("COPY_FAILED")};writer=io.MultiWriter(hash,output)}
  n,err:=io.CopyBuffer(writer,io.LimitReader(input,int64(entry.Size)+1),make([]byte,65536));input.Close();if output!=nil{output.Close()};if err!=nil||uint64(n)!=entry.Size||hex.EncodeToString(hash.Sum(nil))!=entry.SHA256{return nil,fail("PAYLOAD_INVALID")}
 }
 sum:=sha256.Sum256(data);return map[string]any{"ok":true,"bytes":total,"files":len(manifest.Files),"manifest_sha256":hex.EncodeToString(sum[:])},nil
}
func main(){value,e:=run(os.Args[1:]);if e!=nil{value=map[string]any{"ok":false,"code":e.Error()}};data,_:=json.Marshal(value);fmt.Println(string(data));if e!=nil{os.Exit(1)}}
