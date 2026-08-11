Option Explicit

Dim shell, fileSystem, projectPath, pythonwPath, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectPath = fileSystem.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = "D:\Python\Python39\pythonw.exe"
If Not fileSystem.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

shell.CurrentDirectory = projectPath
command = Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & projectPath & "\main.py" & Chr(34)
shell.Run command, 1, False
